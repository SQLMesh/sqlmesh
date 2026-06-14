import agate
from datetime import datetime, timedelta
import json
import logging
import typing as t
from pathlib import Path
from unittest.mock import patch

from sqlmesh.dbt.util import DBT_VERSION

import pytest
from dbt.adapters.base import BaseRelation
from jinja2 import Template

if DBT_VERSION >= (1, 4, 0):
    from dbt.exceptions import CompilationError
else:
    from dbt.exceptions import CompilationException as CompilationError  # type: ignore
import time_machine
from pytest_mock.plugin import MockerFixture
from sqlglot import exp, parse_one
from sqlmesh.core import dialect as d
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core.macros import RuntimeStage
from sqlmesh.core.renderer import render_statements
from sqlmesh.core.audit import StandaloneAudit
from sqlmesh.core.context import Context
from sqlmesh.core.console import get_console
from sqlmesh.core.model import (
    EmbeddedKind,
    FullKind,
    IncrementalByTimeRangeKind,
    IncrementalByUniqueKeyKind,
    IncrementalUnmanagedKind,
    ManagedKind,
    SqlModel,
    ViewKind,
)
from sqlmesh.core.model.kind import (
    SCDType2ByColumnKind,
    SCDType2ByTimeKind,
    OnDestructiveChange,
    OnAdditiveChange,
)
from sqlmesh.core.state_sync.db.snapshot import _snapshot_to_json
from sqlmesh.dbt.builtin import _relation_info_to_relation, Config
from sqlmesh.dbt.common import Dependencies
from sqlmesh.dbt.builtin import _relation_info_to_relation
from sqlmesh.dbt.column import (
    ColumnConfig,
    column_descriptions_to_sqlmesh,
    column_types_to_sqlmesh,
)
from sqlmesh.dbt.context import DbtContext
from sqlmesh.dbt.model import Materialization, ModelConfig
from sqlmesh.dbt.source import SourceConfig
from sqlmesh.dbt.project import Project
from sqlmesh.dbt.relation import Policy
from sqlmesh.dbt.seed import SeedConfig
from sqlmesh.dbt.target import (
    BigQueryConfig,
    DuckDbConfig,
    SnowflakeConfig,
    ClickhouseConfig,
    PostgresConfig,
)
from sqlmesh.dbt.test import TestConfig
from sqlmesh.utils.errors import ConfigError, SQLMeshError
from sqlmesh.utils.jinja import MacroReference

pytestmark = [pytest.mark.dbt, pytest.mark.slow]


def test_model_name(dbt_dummy_postgres_config: PostgresConfig):
    context = DbtContext()
    context._target = dbt_dummy_postgres_config
    assert ModelConfig(schema="foo", path="models/bar.sql").canonical_name(context) == "foo.bar"
    assert (
        ModelConfig(schema="foo", path="models/bar.sql", alias="baz").canonical_name(context)
        == "foo.baz"
    )
    assert (
        ModelConfig(
            database="dbname", schema="foo", path="models/bar.sql", alias="baz"
        ).canonical_name(context)
        == "foo.baz"
    )
    assert (
        ModelConfig(
            database="other", schema="foo", path="models/bar.sql", alias="baz"
        ).canonical_name(context)
        == "other.foo.baz"
    )


def test_materialization():
    context = DbtContext()
    context.project_name = "Test"
    context.target = DuckDbConfig(name="target", schema="foo")

    with patch.object(get_console(), "log_warning") as mock_logger:
        model_config = ModelConfig(
            name="model", alias="model", schema="schema", materialized="materialized_view"
        )

    assert (
        "SQLMesh does not support the 'materialized_view' model materialization. Falling back to the 'view' materialization."
        in mock_logger.call_args[0][0]
    )
    assert model_config.materialized == "view"

    # clickhouse "dictionary" materialization
    with pytest.raises(ConfigError):
        ModelConfig(name="model", alias="model", schema="schema", materialized="dictionary")


def test_dbt_custom_materialization():
    sushi_context = Context(paths=["tests/fixtures/dbt/sushi_test"])

    plan_builder = sushi_context.plan_builder(select_models=["sushi.custom_incremental_model"])
    plan = plan_builder.build()
    assert len(plan.selected_models) == 1
    selected_model = list(plan.selected_models)[0]
    assert selected_model == "model.sushi.custom_incremental_model"

    query = "SELECT * FROM sushi.custom_incremental_model ORDER BY created_at"
    hook_table = "SELECT * FROM hook_table ORDER BY id"
    sushi_context.apply(plan)
    result = sushi_context.engine_adapter.fetchdf(query)
    assert len(result) == 1
    assert {"created_at", "id"}.issubset(result.columns)

    # assert the pre/post hooks executed as well as part of the custom materialization
    hook_result = sushi_context.engine_adapter.fetchdf(hook_table)
    assert len(hook_result) == 1
    assert {"length_col", "id", "updated_at"}.issubset(hook_result.columns)
    assert int(hook_result["length_col"][0]) >= 519
    assert hook_result["id"][0] == 1

    # running with execution time one day in the future to simulate an incremental insert
    tomorrow = datetime.now() + timedelta(days=1)
    sushi_context.run(select_models=["sushi.custom_incremental_model"], execution_time=tomorrow)

    result_after_run = sushi_context.engine_adapter.fetchdf(query)
    assert {"created_at", "id"}.issubset(result_after_run.columns)

    # this should have added new unique values for the new row
    assert len(result_after_run) == 2
    assert result_after_run["id"].is_unique
    assert result_after_run["created_at"].is_unique

    # validate the hooks executed as part of the run as well
    hook_result = sushi_context.engine_adapter.fetchdf(hook_table)
    assert len(hook_result) == 2
    assert hook_result["id"][1] == 2
    assert int(hook_result["length_col"][1]) >= 519
    assert hook_result["id"].is_monotonic_increasing
    assert hook_result["updated_at"].is_unique
    assert not hook_result["length_col"].is_unique


def test_dbt_custom_materialization_with_time_filter_and_macro():
    sushi_context = Context(paths=["tests/fixtures/dbt/sushi_test"])
    today = datetime.now()

    # select both custom materialiasation models with the wildcard
    selector = ["sushi.custom_incremental*"]
    plan_builder = sushi_context.plan_builder(select_models=selector, execution_time=today)
    plan = plan_builder.build()

    assert len(plan.selected_models) == 2
    assert {
        "model.sushi.custom_incremental_model",
        "model.sushi.custom_incremental_with_filter",
    }.issubset(plan.selected_models)

    # the model that daily (default cron) populates with data
    select_daily = "SELECT * FROM sushi.custom_incremental_model ORDER BY created_at"

    # this model uses `run_started_at` as a filter (which we populate with execution time) with 2 day interval
    select_filter = "SELECT * FROM sushi.custom_incremental_with_filter ORDER BY created_at"

    sushi_context.apply(plan)
    result = sushi_context.engine_adapter.fetchdf(select_daily)
    assert len(result) == 1
    assert {"created_at", "id"}.issubset(result.columns)

    result = sushi_context.engine_adapter.fetchdf(select_filter)
    assert len(result) == 1
    assert {"created_at", "id"}.issubset(result.columns)

    # - run ONE DAY LATER
    a_day_later = today + timedelta(days=1)
    sushi_context.run(select_models=selector, execution_time=a_day_later)
    result_after_run = sushi_context.engine_adapter.fetchdf(select_daily)

    # the new row is inserted in the normal incremental model
    assert len(result_after_run) == 2
    assert {"created_at", "id"}.issubset(result_after_run.columns)
    assert result_after_run["id"].is_unique
    assert result_after_run["created_at"].is_unique

    # this model due to the filter shouldn't populate with any new data
    result_after_run_filter = sushi_context.engine_adapter.fetchdf(select_filter)
    assert len(result_after_run_filter) == 1
    assert {"created_at", "id"}.issubset(result_after_run_filter.columns)
    assert result.equals(result_after_run_filter)
    assert result_after_run_filter["id"].is_unique
    assert result_after_run_filter["created_at"][0].date() == today.date()

    # - run TWO DAYS LATER
    two_days_later = a_day_later + timedelta(days=1)
    sushi_context.run(select_models=selector, execution_time=two_days_later)
    result_after_run = sushi_context.engine_adapter.fetchdf(select_daily)

    # again a new row is inserted in the normal model
    assert len(result_after_run) == 3
    assert {"created_at", "id"}.issubset(result_after_run.columns)
    assert result_after_run["id"].is_unique
    assert result_after_run["created_at"].is_unique

    # the model with the filter now should populate as well
    result_after_run_filter = sushi_context.engine_adapter.fetchdf(select_filter)
    assert len(result_after_run_filter) == 2
    assert {"created_at", "id"}.issubset(result_after_run_filter.columns)
    assert result_after_run_filter["id"].is_unique
    assert result_after_run_filter["created_at"][0].date() == today.date()
    assert result_after_run_filter["created_at"][1].date() == two_days_later.date()

    # assert hooks have executed for both plan and incremental runs
    hook_result = sushi_context.engine_adapter.fetchdf("SELECT * FROM hook_table ORDER BY id")
    assert len(hook_result) == 3
    hook_result["id"][0] == 1
    assert hook_result["id"].is_monotonic_increasing
    assert hook_result["updated_at"].is_unique
    assert int(hook_result["length_col"][1]) >= 519
    assert not hook_result["length_col"].is_unique


def test_model_kind():
    context = DbtContext()
    context.project_name = "Test"
    context.target = DuckDbConfig(name="target", schema="foo")

    assert ModelConfig(materialized=Materialization.TABLE).model_kind(context) == FullKind()
    assert ModelConfig(materialized=Materialization.VIEW).model_kind(context) == ViewKind()
    assert ModelConfig(materialized=Materialization.EPHEMERAL).model_kind(context) == EmbeddedKind()
    assert ModelConfig(
        materialized=Materialization.SNAPSHOT,
        unique_key=["id"],
        updated_at="updated_at",
        strategy="timestamp",
    ).model_kind(context) == SCDType2ByTimeKind(
        unique_key=["id"],
        valid_from_name="dbt_valid_from",
        valid_to_name="dbt_valid_to",
        updated_at_as_valid_from=True,
        updated_at_name="updated_at",
        dialect="duckdb",
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.ALLOW,
    )
    assert ModelConfig(
        materialized=Materialization.SNAPSHOT,
        unique_key=["id"],
        strategy="check",
        check_cols=["foo"],
    ).model_kind(context) == SCDType2ByColumnKind(
        unique_key=["id"],
        valid_from_name="dbt_valid_from",
        valid_to_name="dbt_valid_to",
        columns=["foo"],
        execution_time_as_valid_from=True,
        dialect="duckdb",
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.ALLOW,
    )
    assert ModelConfig(
        materialized=Materialization.SNAPSHOT,
        unique_key=["id"],
        strategy="check",
        check_cols=["foo"],
        dialect="bigquery",
    ).model_kind(context) == SCDType2ByColumnKind(
        unique_key=["id"],
        valid_from_name="dbt_valid_from",
        valid_to_name="dbt_valid_to",
        columns=["foo"],
        execution_time_as_valid_from=True,
        dialect="bigquery",
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.ALLOW,
    )

    check_cols_with_cast = ModelConfig(
        materialized=Materialization.SNAPSHOT,
        unique_key=["id"],
        strategy="check",
        check_cols=["created_at::TIMESTAMPTZ"],
    ).model_kind(context)
    assert isinstance(check_cols_with_cast, SCDType2ByColumnKind)
    assert check_cols_with_cast.execution_time_as_valid_from is True
    assert len(check_cols_with_cast.columns) == 1
    assert isinstance(check_cols_with_cast.columns[0], exp.Cast)
    assert check_cols_with_cast.columns[0].sql() == 'CAST("created_at" AS TIMESTAMPTZ)'

    check_cols_multiple_expr = ModelConfig(
        materialized=Materialization.SNAPSHOT,
        unique_key=["id"],
        strategy="check",
        check_cols=["created_at::TIMESTAMPTZ", "COALESCE(status, 'active')"],
    ).model_kind(context)
    assert isinstance(check_cols_multiple_expr, SCDType2ByColumnKind)
    assert len(check_cols_multiple_expr.columns) == 2
    assert isinstance(check_cols_multiple_expr.columns[0], exp.Cast)
    assert isinstance(check_cols_multiple_expr.columns[1], exp.Coalesce)

    assert check_cols_multiple_expr.columns[0].sql() == 'CAST("created_at" AS TIMESTAMPTZ)'
    assert check_cols_multiple_expr.columns[1].sql() == "COALESCE(\"status\", 'active')"

    assert ModelConfig(materialized=Materialization.INCREMENTAL, time_column="foo").model_kind(
        context
    ) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        forward_only=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )
    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        time_column="foo",
        incremental_strategy="delete+insert",
        forward_only=False,
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )
    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        time_column="foo",
        incremental_strategy="insert_overwrite",
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        forward_only=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )
    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        time_column="foo",
        unique_key=["bar"],
        dialect="bigquery",
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="bigquery",
        forward_only=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL, unique_key=["bar"], incremental_strategy="merge"
    ).model_kind(context) == IncrementalByUniqueKeyKind(
        unique_key=["bar"],
        dialect="duckdb",
        forward_only=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    dbt_incremental_predicate = "DBT_INTERNAL_DEST.session_start > dateadd(day, -7, current_date)"
    expected_sqlmesh_predicate = parse_one(
        "__MERGE_TARGET__.session_start > DATEADD(day, -7, CURRENT_DATE)"
    )
    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        unique_key=["bar"],
        incremental_strategy="merge",
        dialect="postgres",
        incremental_predicates=[dbt_incremental_predicate],
    ).model_kind(context) == IncrementalByUniqueKeyKind(
        unique_key=["bar"],
        dialect="postgres",
        forward_only=True,
        disable_restatement=False,
        merge_filter=expected_sqlmesh_predicate,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(materialized=Materialization.INCREMENTAL, unique_key=["bar"]).model_kind(
        context
    ) == IncrementalByUniqueKeyKind(
        unique_key=["bar"],
        dialect="duckdb",
        forward_only=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL, unique_key=["bar"], full_refresh=False
    ).model_kind(context) == IncrementalByUniqueKeyKind(
        unique_key=["bar"],
        dialect="duckdb",
        forward_only=True,
        disable_restatement=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL, unique_key=["bar"], full_refresh=True
    ).model_kind(context) == IncrementalByUniqueKeyKind(
        unique_key=["bar"],
        dialect="duckdb",
        forward_only=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL, unique_key=["bar"], disable_restatement=True
    ).model_kind(context) == IncrementalByUniqueKeyKind(
        unique_key=["bar"],
        dialect="duckdb",
        forward_only=True,
        disable_restatement=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        unique_key=["bar"],
        disable_restatement=True,
        full_refresh=True,
    ).model_kind(context) == IncrementalByUniqueKeyKind(
        unique_key=["bar"],
        dialect="duckdb",
        forward_only=True,
        disable_restatement=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        unique_key=["bar"],
        disable_restatement=True,
        full_refresh=False,
        auto_restatement_cron="0 0 * * *",
    ).model_kind(context) == IncrementalByUniqueKeyKind(
        unique_key=["bar"],
        dialect="duckdb",
        forward_only=True,
        disable_restatement=True,
        auto_restatement_cron="0 0 * * *",
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    # Test incompatibile incremental strategies
    for incremental_strategy in ("delete+insert", "insert_overwrite", "append"):
        assert ModelConfig(
            materialized=Materialization.INCREMENTAL,
            unique_key=["bar"],
            incremental_strategy=incremental_strategy,
        ).model_kind(context) == IncrementalByUniqueKeyKind(
            unique_key=["bar"],
            dialect="duckdb",
            forward_only=True,
            disable_restatement=False,
            on_destructive_change=OnDestructiveChange.IGNORE,
            on_additive_change=OnAdditiveChange.IGNORE,
        )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL, time_column="foo", incremental_strategy="merge"
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        forward_only=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        time_column="foo",
        incremental_strategy="merge",
        full_refresh=True,
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        forward_only=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        time_column="foo",
        incremental_strategy="merge",
        full_refresh=False,
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        forward_only=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        time_column="foo",
        incremental_strategy="append",
        disable_restatement=True,
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        forward_only=True,
        disable_restatement=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        time_column="foo",
        incremental_strategy="insert_overwrite",
        partition_by={"field": "bar"},
        forward_only=False,
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        time_column="foo",
        incremental_strategy="insert_overwrite",
        partition_by={"field": "bar"},
        forward_only=False,
        auto_restatement_cron="0 0 * * *",
        auto_restatement_intervals=3,
    ).model_kind(context) == IncrementalByTimeRangeKind(
        time_column="foo",
        dialect="duckdb",
        forward_only=False,
        auto_restatement_cron="0 0 * * *",
        auto_restatement_intervals=3,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        incremental_strategy="insert_overwrite",
        partition_by={"field": "bar"},
    ).model_kind(context) == IncrementalUnmanagedKind(
        insert_overwrite=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(materialized=Materialization.INCREMENTAL).model_kind(
        context
    ) == IncrementalUnmanagedKind(
        insert_overwrite=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(materialized=Materialization.INCREMENTAL, forward_only=False).model_kind(
        context
    ) == IncrementalUnmanagedKind(
        insert_overwrite=True,
        disable_restatement=False,
        forward_only=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL, incremental_strategy="append"
    ).model_kind(context) == IncrementalUnmanagedKind(
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL, incremental_strategy="append", full_refresh=None
    ).model_kind(context) == IncrementalUnmanagedKind(
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        incremental_strategy="insert_overwrite",
        partition_by={"field": "bar", "data_type": "int64"},
    ).model_kind(context) == IncrementalUnmanagedKind(
        insert_overwrite=True,
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        incremental_strategy="insert_overwrite",
        partition_by={"field": "bar", "data_type": "int64"},
        full_refresh=False,
    ).model_kind(context) == IncrementalUnmanagedKind(
        insert_overwrite=True,
        disable_restatement=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        incremental_strategy="insert_overwrite",
        partition_by={"field": "bar", "data_type": "int64"},
        disable_restatement=True,
        full_refresh=True,
    ).model_kind(context) == IncrementalUnmanagedKind(
        insert_overwrite=True,
        disable_restatement=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        incremental_strategy="insert_overwrite",
        partition_by={"field": "bar", "data_type": "int64"},
        disable_restatement=True,
    ).model_kind(context) == IncrementalUnmanagedKind(
        insert_overwrite=True,
        disable_restatement=True,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert ModelConfig(
        materialized=Materialization.INCREMENTAL,
        incremental_strategy="insert_overwrite",
        auto_restatement_cron="0 0 * * *",
    ).model_kind(context) == IncrementalUnmanagedKind(
        insert_overwrite=True,
        auto_restatement_cron="0 0 * * *",
        disable_restatement=False,
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.IGNORE,
    )

    assert (
        ModelConfig(materialized=Materialization.DYNAMIC_TABLE, target_lag="1 hour").model_kind(
            context
        )
        == ManagedKind()
    )

    assert ModelConfig(
        materialized=Materialization.SNAPSHOT,
        unique_key=["id"],
        updated_at="updated_at::timestamp",
        strategy="timestamp",
        dialect="redshift",
    ).model_kind(context) == SCDType2ByTimeKind(
        unique_key=["id"],
        valid_from_name="dbt_valid_from",
        valid_to_name="dbt_valid_to",
        updated_at_as_valid_from=True,
        updated_at_name="updated_at",
        dialect="redshift",
        on_destructive_change=OnDestructiveChange.IGNORE,
        on_additive_change=OnAdditiveChange.ALLOW,
    )


def test_model_kind_snapshot_bigquery():
    context = DbtContext()
    context.project_name = "Test"
    context.target = BigQueryConfig(name="target", schema="foo", project="bar")

    assert ModelConfig(
        materialized=Materialization.SNAPSHOT,
        unique_key=["id"],
        updated_at="updated_at",
        strategy="timestamp",
    ).model_kind(context) == SCDType2ByTimeKind(
        unique_key=["id"],
        valid_from_name="dbt_valid_from",
        valid_to_name="dbt_valid_to",
        updated_at_as_valid_from=True,
        updated_at_name="updated_at",
        time_data_type=exp.DataType.build("TIMESTAMPTZ"),
        dialect="bigquery",
        on_destructive_change=OnDestructiveChange.IGNORE,
    )

    # time_data_type is bigquery version even though model dialect is DuckDB
    # because model target is BigQuery
    assert ModelConfig(
        materialized=Materialization.SNAPSHOT,
        unique_key=["id"],
        updated_at="updated_at",
        strategy="timestamp",
        dialect="duckdb",
    ).model_kind(context) == SCDType2ByTimeKind(
        unique_key=["id"],
        valid_from_name="dbt_valid_from",
        valid_to_name="dbt_valid_to",
        updated_at_as_valid_from=True,
        updated_at_name="updated_at",
        time_data_type=exp.DataType.build("TIMESTAMPTZ"),  # bigquery version
        dialect="duckdb",
        on_destructive_change=OnDestructiveChange.IGNORE,
    )


def test_model_columns():
    model = ModelConfig(
        alias="test",
        target_schema="foo",
        table_name="bar",
        sql="SELECT * FROM baz",
        columns={
            "ADDRESS": ColumnConfig(
                name="address", data_type="text", description="Business address"
            ),
            "ZIPCODE": ColumnConfig(
                name="zipcode", data_type="varchar(5)", description="Business zipcode"
            ),
            "DATE": ColumnConfig(
                name="date", data_type="timestamp_ntz", description="Contract date"
            ),
        },
    )

    expected_column_types = {
        "ADDRESS": exp.DataType.build("text"),
        "ZIPCODE": exp.DataType.build("varchar(5)"),
        "DATE": exp.DataType.build("timestamp_ntz", dialect="snowflake"),
    }
    expected_column_descriptions = {
        "ADDRESS": "Business address",
        "ZIPCODE": "Business zipcode",
        "DATE": "Contract date",
    }

    assert column_types_to_sqlmesh(model.columns, "snowflake") == expected_column_types
    assert column_descriptions_to_sqlmesh(model.columns) == expected_column_descriptions

    context = DbtContext()
    context.project_name = "Foo"
    context.target = SnowflakeConfig(
        name="target", schema="test", database="test", account="foo", user="bar", password="baz"
    )
    sqlmesh_model = model.to_sqlmesh(context)

    # Columns being present in a schema.yaml are not respected in DDLs, so SQLMesh doesn't
    # set the corresponding columns_to_types_ attribute either to match dbt's behavior
    assert sqlmesh_model.columns_to_types == None
    assert sqlmesh_model.column_descriptions == expected_column_descriptions


def test_seed_columns():
    seed = SeedConfig(
        name="foo",
        package="package",
        path=Path("examples/sushi_dbt/seeds/waiter_names.csv"),
        columns={
            "id": ColumnConfig(name="id", data_type="text", description="The ID"),
            "name": ColumnConfig(name="name", data_type="text", description="The name"),
        },
    )

    # dbt doesn't respect the data_type field in the DDLs– instead, it optionally uses it to
    # validate the actual data types at runtime through contracts or external plugins. Thus,
    # the actual data type is int, because that is what is inferred from the seed file.
    expected_column_types = {
        "id": exp.DataType.build("int"),
        "name": exp.DataType.build("text"),
    }
    expected_column_descriptions = {
        "id": "The ID",
        "name": "The name",
    }

    context = DbtContext()
    context.project_name = "Foo"
    context.target = DuckDbConfig(name="target", schema="test")
    sqlmesh_seed = seed.to_sqlmesh(context)
    assert sqlmesh_seed.columns_to_types == expected_column_types
    assert sqlmesh_seed.column_descriptions == expected_column_descriptions


def test_seed_column_types():
    seed = SeedConfig(
        name="foo",
        package="package",
        path=Path("examples/sushi_dbt/seeds/waiter_names.csv"),
        column_types={
            "id": "text",
            "name": "text",
        },
        columns={
            "name": ColumnConfig(name="name", description="The name"),
        },
        quote_columns=True,
    )

    expected_column_types = {
        "id": exp.DataType.build("text"),
        "name": exp.DataType.build("text"),
    }
    expected_column_descriptions = {
        "name": "The name",
    }

    context = DbtContext()
    context.project_name = "Foo"
    context.target = DuckDbConfig(name="target", schema="test")
    sqlmesh_seed = seed.to_sqlmesh(context)

    assert sqlmesh_seed.columns_to_types == expected_column_types
    assert sqlmesh_seed.column_descriptions == expected_column_descriptions

    seed = SeedConfig(
        name="foo",
        package="package",
        path=Path("examples/sushi_dbt/seeds/waiter_names.csv"),
        column_types={
            "name": "text",
        },
        columns={
            # The `data_type` field does not affect the materialized seed's column type
            "id": ColumnConfig(name="name", data_type="text"),
        },
        quote_columns=True,
    )

    expected_column_types = {
        "id": exp.DataType.build("int"),
        "name": exp.DataType.build("text"),
    }
    sqlmesh_seed = seed.to_sqlmesh(context)
    assert sqlmesh_seed.columns_to_types == expected_column_types

    seed = SeedConfig(
        name="foo",
        package="package",
        path=Path("examples/sushi_dbt/seeds/waiter_names.csv"),
        column_types={
            "id": "TEXT",
            "name": "TEXT NOT NULL",
        },
        quote_columns=True,
    )

    expected_column_types = {
        "id": exp.DataType.build("text"),
        "name": exp.DataType.build("text"),
    }

    logger = logging.getLogger("sqlmesh.dbt.column")
    with patch.object(logger, "warning") as mock_logger:
        sqlmesh_seed = seed.to_sqlmesh(context)
    assert "Ignoring unsupported constraints" in mock_logger.call_args[0][0]
    assert sqlmesh_seed.columns_to_types == expected_column_types


def test_seed_column_inference(tmp_path):
    seed_csv = tmp_path / "seed.csv"
    with open(seed_csv, "w", encoding="utf-8") as fd:
        fd.write("int_col,double_col,datetime_col,date_col,boolean_col,text_col\n")
        fd.write("1,1.2,2021-01-01 00:00:00,2021-01-01,true,foo\n")
        fd.write("2,2.3,2021-01-02 00:00:00,2021-01-02,false,bar\n")
        fd.write("null,,null,,,null\n")

    seed = SeedConfig(
        name="test_model",
        package="package",
        path=Path(seed_csv),
    )

    context = DbtContext()
    context.project_name = "Foo"
    context.target = DuckDbConfig(name="target", schema="test")
    sqlmesh_seed = seed.to_sqlmesh(context)
    assert sqlmesh_seed.columns_to_types == {
        "int_col": exp.DataType.build("int")
        if DBT_VERSION >= (1, 8, 0)
        else exp.DataType.build("double"),
        "double_col": exp.DataType.build("double"),
        "datetime_col": exp.DataType.build("datetime"),
        "date_col": exp.DataType.build("date"),
        "boolean_col": exp.DataType.build("boolean"),
        "text_col": exp.DataType.build("text"),
    }


def test_seed_single_whitespace_is_na(tmp_path):
    seed_csv = tmp_path / "seed.csv"
    with open(seed_csv, "w", encoding="utf-8") as fd:
        fd.write("col_a, col_b\n")
        fd.write(" ,1\n")
        fd.write("2, \n")

    seed = SeedConfig(
        name="test_model",
        package="foo",
        path=Path(seed_csv),
    )

    context = DbtContext()
    context.project_name = "foo"
    context.target = DuckDbConfig(name="target", schema="test")
    sqlmesh_seed = seed.to_sqlmesh(context)
    assert sqlmesh_seed.columns_to_types == {
        "col_a": exp.DataType.build("int"),
        "col_b": exp.DataType.build("int"),
    }

    df = next(sqlmesh_seed.render_seed())
    assert df["col_a"].to_list() == [None, 2]
    assert df["col_b"].to_list() == [1, None]


def test_seed_partial_column_inference(tmp_path):
    seed_csv = tmp_path / "seed.csv"
    with open(seed_csv, "w", encoding="utf-8") as fd:
        fd.write("int_col,double_col,datetime_col,boolean_col\n")
        fd.write("1,1.2,2021-01-01 00:00:00,true\n")
        fd.write("2,2.3,2021-01-02 00:00:00,false\n")
        fd.write("null,,null,\n")

    seed = SeedConfig(
        name="test_model",
        package="package",
        path=Path(seed_csv),
        column_types={
            "double_col": "double",
        },
        columns={
            "int_col": ColumnConfig(
                name="int_col", data_type="int", description="Description with type."
            ),
            "datetime_col": ColumnConfig(
                name="datetime_col", description="Description without type."
            ),
            "boolean_col": ColumnConfig(name="boolean_col"),
        },
    )

    expected_column_types = {
        "int_col": exp.DataType.build("int"),
        "double_col": exp.DataType.build("double"),
        "datetime_col": exp.DataType.build("datetime"),
        "boolean_col": exp.DataType.build("boolean"),
    }

    expected_column_descriptions = {
        "int_col": "Description with type.",
        "datetime_col": "Description without type.",
    }

    context = DbtContext()
    context.project_name = "Foo"
    context.target = DuckDbConfig(name="target", schema="test")
    sqlmesh_seed = seed.to_sqlmesh(context)
    assert sqlmesh_seed.columns_to_types == expected_column_types
    assert sqlmesh_seed.column_descriptions == expected_column_descriptions

    # Check that everything still lines up
    seed_df = next(sqlmesh_seed.render_seed())
    assert list(seed_df.columns) == list(sqlmesh_seed.columns_to_types.keys())


def test_seed_delimiter(tmp_path):
    seed_csv = tmp_path / "seed_with_delimiter.csv"

    with open(seed_csv, "w", encoding="utf-8") as fd:
        fd.writelines("\n".join(["id|name|city", "0|Ayrton|SP", "1|Max|MC", "2|Niki|VIE"]))

    seed = SeedConfig(
        name="test_model_pipe",
        package="package",
        path=Path(seed_csv),
        delimiter="|",
    )

    context = DbtContext()
    context.project_name = "TestProject"
    context.target = DuckDbConfig(name="target", schema="test")
    sqlmesh_seed = seed.to_sqlmesh(context)

    # Verify columns are correct with the custom pipe (|) delimiter
    expected_columns = {"id", "name", "city"}
    assert set(sqlmesh_seed.columns_to_types.keys()) == expected_columns

    seed_df = next(sqlmesh_seed.render_seed())
    assert list(seed_df.columns) == list(sqlmesh_seed.columns_to_types.keys())
    assert len(seed_df) == 3

    assert seed_df.iloc[0]["name"] == "Ayrton"
    assert seed_df.iloc[0]["city"] == "SP"
    assert seed_df.iloc[1]["name"] == "Max"
    assert seed_df.iloc[1]["city"] == "MC"

    # test with semicolon delimiter
    seed_csv_semicolon = tmp_path / "seed_with_semicolon.csv"
    with open(seed_csv_semicolon, "w", encoding="utf-8") as fd:
        fd.writelines("\n".join(["id;value;status", "1;100;active", "2;200;inactive"]))

    seed_semicolon = SeedConfig(
        name="test_model_semicolon",
        package="package",
        path=Path(seed_csv_semicolon),
        delimiter=";",
    )

    sqlmesh_seed_semicolon = seed_semicolon.to_sqlmesh(context)
    expected_columns_semicolon = {"id", "value", "status"}
    assert set(sqlmesh_seed_semicolon.columns_to_types.keys()) == expected_columns_semicolon

    seed_df_semicolon = next(sqlmesh_seed_semicolon.render_seed())
    assert seed_df_semicolon.iloc[0]["value"] == 100
    assert seed_df_semicolon.iloc[0]["status"] == "active"


def test_seed_column_order(tmp_path):
    seed_csv = tmp_path / "seed.csv"

    with open(seed_csv, "w", encoding="utf-8") as fd:
        fd.writelines("\n".join(["id,name", "0,Toby", "1,Tyson", "2,Ryan"]))

    seed = SeedConfig(
        name="test_model",
        package="package",
        path=Path(seed_csv),
        columns={
            "id": ColumnConfig(name="id"),
            "name": ColumnConfig(name="name", data_type="varchar"),
        },
    )

    context = DbtContext()
    context.project_name = "Foo"
    context.target = DuckDbConfig(name="target", schema="test")
    sqlmesh_seed = seed.to_sqlmesh(context)

    # Check that everything still lines up
    seed_df = next(sqlmesh_seed.render_seed())
    assert list(seed_df.columns) == list(sqlmesh_seed.columns_to_types.keys())


def test_agate_integer_cast():
    # Not all dbt versions have agate.Integer
    if DBT_VERSION < (1, 7, 0):
        pytest.skip("agate.Integer not available")

    from sqlmesh.dbt.seed import Integer

    agate_integer = Integer(null_values=("null", ""))
    assert agate_integer.cast("1") == 1
    assert agate_integer.cast(1) == 1
    assert agate_integer.cast("null") is None
    assert agate_integer.cast("") is None

    with pytest.raises(agate.exceptions.CastError):
        agate_integer.cast("1.2")

    with pytest.raises(agate.exceptions.CastError):
        agate_integer.cast(1.2)

    with pytest.raises(agate.exceptions.CastError):
        agate_integer.cast(datetime.now())


@pytest.mark.xdist_group("dbt_manifest")
def test_model_dialect(sushi_test_project: Project, assert_exp_eq):
    model_config = ModelConfig(
        name="model",
        package_name="package",
        schema="sushi",
        alias="table",
        sql="SELECT 1 AS `one` FROM {{ schema }}",
    )
    context = sushi_test_project.context

    # cannot parse model sql without specifying bigquery dialect
    with pytest.raises(ConfigError):
        model_config.to_sqlmesh(context).render_query_or_raise().sql()

    model_config = ModelConfig(
        name="model",
        package_name="package",
        schema="sushi",
        alias="table",
        sql="SELECT 1 AS `one` FROM {{ schema }}",
        dialect="bigquery",
    )
    assert_exp_eq(
        model_config.to_sqlmesh(context).render_query_or_raise().sql(),
        'SELECT 1 AS "one" FROM "sushi" AS "sushi"',
    )


@pytest.mark.xdist_group("dbt_manifest")
@pytest.mark.parametrize(
    "model_fqn", ['"memory"."sushi"."waiters"', '"memory"."sushi"."waiter_names"']
)
def test_hooks(sushi_test_dbt_context: Context, model_fqn: str):
    engine_adapter = sushi_test_dbt_context.engine_adapter
    waiters = sushi_test_dbt_context.models[model_fqn]

    logger = logging.getLogger("sqlmesh.dbt.builtin")
    with patch.object(logger, "debug") as mock_logger:
        engine_adapter.execute(
            waiters.render_pre_statements(
                engine_adapter=engine_adapter, execution_time="2023-01-01"
            )
        )
    assert "pre-hook" in mock_logger.call_args[0][0]

    with patch.object(logger, "debug") as mock_logger:
        engine_adapter.execute(
            waiters.render_post_statements(
                engine_adapter=sushi_test_dbt_context.engine_adapter, execution_time="2023-01-01"
            )
        )
    assert "post-hook" in mock_logger.call_args[0][0]


@pytest.mark.xdist_group("dbt_manifest")
def test_seed_delimiter_integration(sushi_test_dbt_context: Context):
    seed_fqn = '"memory"."sushi"."waiter_revenue_semicolon"'
    assert seed_fqn in sushi_test_dbt_context.models

    seed_model = sushi_test_dbt_context.models[seed_fqn]
    assert seed_model.columns_to_types is not None

    # this should be loaded with semicolon delimiter otherwise it'd resylt in an one column table
    assert set(seed_model.columns_to_types.keys()) == {"waiter_id", "revenue", "quarter"}

    # columns_to_types values are correct types as well
    assert seed_model.columns_to_types == {
        "waiter_id": exp.DataType.build("int"),
        "revenue": exp.DataType.build("double"),
        "quarter": exp.DataType.build("text"),
    }

    df = sushi_test_dbt_context.fetchdf(f"SELECT * FROM {seed_fqn}")

    assert len(df) == 6
    waiter_ids = set(df["waiter_id"].tolist())
    quarters = set(df["quarter"].tolist())
    assert waiter_ids == {1, 2, 3}
    assert quarters == {"Q1", "Q2"}

    q1_w1_rows = df[(df["waiter_id"] == 1) & (df["quarter"] == "Q1")]
    assert len(q1_w1_rows) == 1
    assert float(q1_w1_rows.iloc[0]["revenue"]) == 100.50

    q2_w2_rows = df[(df["waiter_id"] == 2) & (df["quarter"] == "Q2")]
    assert len(q2_w2_rows) == 1
    assert float(q2_w2_rows.iloc[0]["revenue"]) == 225.50

    q2_w3_rows = df[(df["waiter_id"] == 3) & (df["quarter"] == "Q2")]
    assert len(q2_w3_rows) == 1
    assert float(q2_w3_rows.iloc[0]["revenue"]) == 175.75


@pytest.mark.xdist_group("dbt_manifest")
def test_target_jinja(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ target.name }}") == "in_memory"
    assert context.render("{{ target.schema }}") == "sushi"
    assert context.render("{{ target.type }}") == "duckdb"
    # Path and Profile name are not included in serializable fields
    assert context.render("{{ target.path }}") == "None"
    assert context.render("{{ target.profile_name }}") == "None"

    context = DbtContext()
    context._target = SnowflakeConfig(
        name="target",
        schema="test",
        database="test",
        account="account",
        user="user",
        password="password",
        warehouse="warehouse",
        role="role",
        threads=1,
    )
    assert context.render("{{ target.threads }}") == "1"
    assert context.render("{{ target.database }}") == "test"
    assert context.render("{{ target.warehouse }}") == "warehouse"
    assert context.render("{{ target.user }}") == "user"
    assert context.render("{{ target.role }}") == "role"
    assert context.render("{{ target.account }}") == "account"

    context = DbtContext()
    context._target = PostgresConfig(
        name="target",
        schema="test",
        database="test",
        dbname="test",
        host="host",
        port=5432,
        user="user",
        password="password",
    )
    assert context.render("{{ target.dbname }}") == "test"
    assert context.render("{{ target.host }}") == "host"
    assert context.render("{{ target.port }}") == "5432"

    context = DbtContext()
    context._target = BigQueryConfig(
        name="target",
        schema="test_value",
        database="test_project",
    )
    assert context.render("{{ target.project }}") == "test_project"
    assert context.render("{{ target.database }}") == "test_project"
    assert context.render("{{ target.schema }}") == "test_value"
    assert context.render("{{ target.dataset }}") == "test_value"


@pytest.mark.xdist_group("dbt_manifest")
def test_project_name_jinja(sushi_test_project: Project):
    context = sushi_test_project.context
    assert context.render("{{ project_name }}") == "sushi"


@pytest.mark.xdist_group("dbt_manifest")
def test_schema_jinja(sushi_test_project: Project, assert_exp_eq):
    model_config = ModelConfig(
        name="model",
        package_name="package",
        schema="sushi",
        alias="table",
        sql="SELECT 1 AS one FROM {{ schema }}",
    )
    context = sushi_test_project.context
    assert_exp_eq(
        model_config.to_sqlmesh(context).render_query_or_raise().sql(),
        'SELECT 1 AS "one" FROM "sushi" AS "sushi"',
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_config_jinja(sushi_test_project: Project):
    hook = "{{ config(alias='bar') }} {{ config.get('alias') }}"
    model_config = ModelConfig(
        name="model",
        package_name="package",
        schema="sushi",
        sql="""SELECT 1 AS one FROM foo""",
        alias="model",
        **{"pre-hook": hook},
    )
    context = sushi_test_project.context
    model = t.cast(SqlModel, model_config.to_sqlmesh(context))
    assert hook in model.pre_statements[0].sql()
    assert model.render_pre_statements()[0].sql() == '"bar"'


@pytest.mark.xdist_group("dbt_manifest")
def test_config_dict_syntax():
    # Test dictionary syntax
    config = Config({})
    result = config({"materialized": "table", "alias": "dict_table"})
    assert result == ""
    assert config._config["materialized"] == "table"
    assert config._config["alias"] == "dict_table"

    # Test kwargs syntax still works
    config2 = Config({})
    result = config2(materialized="view", alias="kwargs_table")
    assert result == ""
    assert config2._config["materialized"] == "view"
    assert config2._config["alias"] == "kwargs_table"

    # Test that mixing args and kwargs is rejected
    config3 = Config({})
    try:
        config3({"materialized": "table"}, alias="mixed")
        assert False, "Should have raised ConfigError"
    except Exception as e:
        assert "cannot mix positional and keyword arguments" in str(e)

    # Test nested dicts
    config4 = Config({})
    config4({"meta": {"owner": "data_team", "priority": 1}, "tags": ["daily", "critical"]})
    assert config4._config["meta"]["owner"] == "data_team"
    assert config4._config["tags"] == ["daily", "critical"]

    # Test multiple positional arguments are rejected
    config4 = Config({})
    try:
        config4({"materialized": "table"}, {"alias": "test"})
        assert False
    except Exception as e:
        assert "expected a single dictionary, got 2 arguments" in str(e)


def test_config_dict_in_jinja():
    # Test dict syntax directly with Config class
    config = Config({})
    template = Template("{{ config({'materialized': 'table', 'unique_key': 'id'}) }}done")
    result = template.render(config=config)
    assert result == "done"
    assert config._config["materialized"] == "table"
    assert config._config["unique_key"] == "id"

    # Test with nested dict and list values
    config2 = Config({})
    complex_template = Template("""{{ config({
        'tags': ['test', 'dict'],
        'meta': {'owner': 'data_team'}
    }) }}result""")
    result = complex_template.render(config=config2)
    assert result == "result"
    assert config2._config["tags"] == ["test", "dict"]
    assert config2._config["meta"]["owner"] == "data_team"

    # Test that kwargs still work
    config3 = Config({})
    kwargs_template = Template("{{ config(materialized='view', alias='my_view') }}done")
    result = kwargs_template.render(config=config3)
    assert result == "done"
    assert config3._config["materialized"] == "view"
    assert config3._config["alias"] == "my_view"


@pytest.mark.xdist_group("dbt_manifest")
def test_config_dict_syntax_in_sushi_project(sushi_test_project: Project):
    assert sushi_test_project is not None
    assert sushi_test_project.context is not None

    sushi_package = sushi_test_project.packages.get("sushi")
    assert sushi_package is not None

    top_waiters_found = False
    for model_config in sushi_package.models.values():
        if model_config.name == "top_waiters":
            # top_waiters model now uses dict config syntax with:
            # config({'materialized': 'view', 'limit_value': var('top_waiters:limit'), 'meta': {...}})
            top_waiters_found = True
            assert model_config.materialized == "view"
            assert model_config.meta is not None
            assert model_config.meta.get("owner") == "analytics_team"
            assert model_config.meta.get("priority") == "high"
            break

    assert top_waiters_found


@pytest.mark.xdist_group("dbt_manifest")
def test_config_jinja_get_methods(sushi_test_project: Project):
    model_config = ModelConfig(
        name="model_conf",
        package_name="package",
        schema="sushi",
        sql="""SELECT 1 AS one FROM foo""",
        alias="model_alias",
        **{
            "pre-hook": [
                "{{ config(materialized='incremental', unique_key='id') }}"
                "{{ config.get('missed', 'a') + config.get('missed', default='b')}}",
                "{{ config.set('alias', 'new_alias')}}",
                "{{ config.get('package_name') + '_' + config.require('unique_key')}}",
                "{{ config.get('alias') or 'default'}}",
            ]
        },
        **{"post-hook": "{{config.require('missing_key')}}"},
    )
    context = sushi_test_project.context
    model = t.cast(SqlModel, model_config.to_sqlmesh(context))

    assert model.render_pre_statements()[0].sql() == '"ab"'
    assert model.render_pre_statements()[1].sql() == '"package_id"'
    assert model.render_pre_statements()[2].sql() == '"new_alias"'

    with pytest.raises(ConfigError, match="Missing required config: missing_key"):
        model.render_post_statements()

    # test get methods with operations
    model_2_config = ModelConfig(
        name="model_2",
        package_name="package",
        schema="sushi",
        sql="""SELECT 1 AS one FROM foo""",
        alias="mod",
        materialized="table",
        threads=8,
        partition_by="date",
        cluster_by=["user_id", "product_id"],
        **{
            "pre-hook": [
                "{{ config.get('partition_by', default='none') }}",
                "{{ config.get('cluster_by', default=[]) | length }}",
                "{% if config.get('threads') > 4 %}high_threads{% else %}low_threads{% endif %}",
            ]
        },
    )
    model2 = t.cast(SqlModel, model_2_config.to_sqlmesh(context))

    pre_statements2 = model2.render_pre_statements()
    assert pre_statements2[0].sql() == "ARRAY('date')"
    assert pre_statements2[1].sql() == "2"
    assert pre_statements2[2].sql() == '"high_threads"'

    # test seting variable and conditional
    model_invalid_timeout = ModelConfig(
        name="invalid_timeout_test",
        package_name="package",
        schema="sushi",
        sql="""SELECT 1 AS one FROM foo""",
        alias="invalid_timeout_alias",
        connection_timeout=44,
        **{
            "pre-hook": [
                """
            {%- set value = config.require('connection_timeout') -%}
            {%- set is_valid = value >= 10 and value <= 30 -%}
            {%- if not is_valid -%}
              {{ exceptions.raise_compiler_error("Validation failed for 'connection_timeout': Value must be between 10 and 30, got: " ~ value) }}
            {%- endif -%}
            {{ value }}
            """,
            ]
        },
    )

    model_invalid = t.cast(SqlModel, model_invalid_timeout.to_sqlmesh(context))
    with pytest.raises(
        ConfigError,
        match="Validation failed for 'connection_timeout': Value must be between 10 and 30, got: 44",
    ):
        model_invalid.render_pre_statements()

    # test persist_docs methods
    model_config_persist = ModelConfig(
        name="persist_docs_model",
        package_name="package",
        schema="sushi",
        sql="""SELECT 1 AS one FROM foo""",
        alias="persist_alias",
        **{
            "pre-hook": [
                "{{ config(persist_docs={'relation': true, 'columns': true}) }}",
                "{{ config.persist_relation_docs() }}",
                "{{ config.persist_column_docs() }}",
                "{{ config(persist_docs={'relation': false, 'columns': true}) }}",
                "{{ config.persist_relation_docs() }}",
                "{{ config.persist_column_docs() }}",
            ]
        },
    )
    model3 = t.cast(SqlModel, model_config_persist.to_sqlmesh(context))

    pre_statements3 = model3.render_pre_statements()

    # it should filter out empty returns, so we get 4 statements
    assert len(pre_statements3) == 4
    assert pre_statements3[0].sql() == "TRUE"
    assert pre_statements3[1].sql() == "TRUE"
    assert pre_statements3[2].sql() == "FALSE"
    assert pre_statements3[3].sql() == "TRUE"


@pytest.mark.xdist_group("dbt_manifest")
def test_model_this(assert_exp_eq, sushi_test_project: Project):
    model_config = ModelConfig(
        name="model",
        package_name="package",
        schema="schema",
        alias="test",
        sql="SELECT 1 AS one FROM {{ this.identifier }}",
    )
    context = sushi_test_project.context
    assert_exp_eq(
        model_config.to_sqlmesh(context).render_query_or_raise().sql(),
        'SELECT 1 AS "one" FROM "test" AS "test"',
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_test_this(assert_exp_eq, sushi_test_project: Project):
    test_config = TestConfig(
        name="test",
        alias="alias",
        database="database",
        schema_="schema",
        standalone=True,
        sql="SELECT 1 AS one FROM {{ this.identifier }}",
    )
    context = sushi_test_project.context
    audit = t.cast(StandaloneAudit, test_config.to_sqlmesh(context))
    assert_exp_eq(
        audit.render_audit_query().sql(),
        'SELECT 1 AS "one" FROM "test" AS "test"',
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_test_dialect(assert_exp_eq, sushi_test_project: Project):
    test_config = TestConfig(
        name="test",
        alias="alias",
        database="database",
        schema_="schema",
        standalone=True,
        sql="SELECT 1 AS `one` FROM {{ this.identifier }}",
    )
    context = sushi_test_project.context

    # can't parse test sql without specifying bigquery as default dialect
    with pytest.raises(ConfigError):
        audit = t.cast(StandaloneAudit, test_config.to_sqlmesh(context))
        audit.render_audit_query().sql()

    test_config.dialect_ = "bigquery"
    audit = t.cast(StandaloneAudit, test_config.to_sqlmesh(context))
    assert_exp_eq(
        audit.render_audit_query().sql(),
        'SELECT 1 AS "one" FROM "test" AS "test"',
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_statement(sushi_test_project: Project, runtime_renderer: t.Callable):
    context = sushi_test_project.context
    assert context.target
    engine_adapter = context.target.to_sqlmesh().create_engine_adapter()
    renderer = runtime_renderer(context, engine_adapter=engine_adapter)
    assert (
        renderer(
            "{% set test_var = 'SELECT 1' %}{% call statement('something', fetch_result=True) %} {{ test_var }} {% endcall %}{{ load_result('something').table }}",
        )
        == """| column | data_type |
| ------ | --------- |
| 1      | Integer   |
"""
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_run_query(sushi_test_project: Project, runtime_renderer: t.Callable):
    context = sushi_test_project.context
    assert context.target
    engine_adapter = context.target.to_sqlmesh().create_engine_adapter()
    renderer = runtime_renderer(context, engine_adapter=engine_adapter)
    assert (
        renderer(
            """{% set results = run_query('SELECT 1 UNION ALL SELECT 2') %}{% for val in results.columns[0] %}{{ val }} {% endfor %}"""
        )
        == "1 2 "
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_logging(sushi_test_project: Project, runtime_renderer: t.Callable):
    context = sushi_test_project.context
    assert context.target
    engine_adapter = context.target.to_sqlmesh().create_engine_adapter()
    renderer = runtime_renderer(context, engine_adapter=engine_adapter)

    logger = logging.getLogger("sqlmesh.dbt.builtin")

    # Test log with info=False (default), should only log to file with debug and not to console
    with (
        patch.object(logger, "debug") as mock_debug,
        patch.object(logger, "info") as mock_info,
        patch.object(get_console(), "log_status_update") as mock_console,
    ):
        assert renderer('{{ log("foo") }}') == ""
        mock_debug.assert_called_once()
        assert "foo" in mock_debug.call_args[0][0]
        mock_info.assert_not_called()
        mock_console.assert_not_called()

    # Test log with info=True, should log to info and also call log_status_update
    with (
        patch.object(logger, "debug") as mock_debug,
        patch.object(logger, "info") as mock_info,
        patch.object(get_console(), "log_status_update") as mock_console,
    ):
        assert renderer('{{ log("output to be logged with info", info=true) }}') == ""
        mock_info.assert_called_once()
        assert "output to be logged with info" in mock_info.call_args[0][0]
        mock_debug.assert_not_called()
        mock_console.assert_called_once()
        assert "output to be logged with info" in mock_console.call_args[0][0]

    # Test print function as well, should use debug
    with (
        patch.object(logger, "debug") as mock_logger,
        patch.object(get_console(), "log_status_update") as mock_console,
    ):
        assert renderer('{{ print("bar") }}') == ""
        assert "bar" in mock_logger.call_args[0][0]
        mock_console.assert_not_called()


@pytest.mark.xdist_group("dbt_manifest")
def test_exceptions(sushi_test_project: Project):
    context = sushi_test_project.context

    logger = logging.getLogger("sqlmesh.dbt.builtin")
    with patch.object(logger, "warning") as mock_logger:
        assert context.render('{{ exceptions.warn("Warning") }}') == ""
    assert "Warning" in mock_logger.call_args[0][0]

    with pytest.raises(CompilationError, match="Error"):
        context.render('{{ exceptions.raise_compiler_error("Error") }}')


@pytest.mark.xdist_group("dbt_manifest")
def test_try_or_compiler_error(sushi_test_project: Project):
    context = sushi_test_project.context

    result = context.render(
        '{{ try_or_compiler_error("Error message", modules.datetime.datetime.strptime, "2023-01-15", "%Y-%m-%d") }}'
    )
    assert "2023-01-15" in result

    with pytest.raises(CompilationError, match="Invalid date format"):
        context.render(
            '{{ try_or_compiler_error("Invalid date format", modules.datetime.datetime.strptime, "invalid", "%Y-%m-%d") }}'
        )

    # built-in macro calling try_or_compiler_error works
    result = context.render(
        '{{ dbt.dates_in_range("2023-01-01", "2023-01-03", "%Y-%m-%d", "%Y-%m-%d") }}'
    )
    assert "2023-01-01" in result
    assert "2023-01-02" in result
    assert "2023-01-03" in result


@pytest.mark.xdist_group("dbt_manifest")
def test_modules(sushi_test_project: Project):
    context = sushi_test_project.context

    # datetime
    assert context.render("{{ modules.datetime.date(2022, 12, 25) }}") == "2022-12-25"

    # pytz
    try:
        assert "UTC" in context.render("{{ modules.pytz.all_timezones }}")
    except AttributeError as error:
        assert "object has no attribute 'pytz'" in str(error)

    # re
    assert context.render("{{ modules.re.search('(?<=abc)def', 'abcdef').group(0) }}") == "def"

    # itertools
    itertools_jinja = "{% for num in modules.itertools.accumulate([5]) %}{{ num }}{% endfor %}"
    assert context.render(itertools_jinja) == "5"


@pytest.mark.xdist_group("dbt_manifest")
def test_flags(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ flags.FULL_REFRESH }}") == "None"
    assert context.render("{{ flags.STORE_FAILURES }}") == "None"
    assert context.render("{{ flags.WHICH }}") == "parse"


def test_invocation_args_dict(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ invocation_args_dict['full_refresh'] }}") == "None"
    assert context.render("{{ invocation_args_dict['store_failures'] }}") == "None"
    assert context.render("{{ invocation_args_dict['which'] }}") == "parse"


@pytest.mark.xdist_group("dbt_manifest")
def test_context_namespace(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ context.flags.FULL_REFRESH }}") == "None"


@pytest.mark.xdist_group("dbt_manifest")
def test_relation(sushi_test_project: Project):
    context = sushi_test_project.context

    assert (
        context.render("{{ api.Relation }}")
        == "<class 'dbt.adapters.duckdb.relation.DuckDBRelation'>"
    )

    jinja = (
        "{% set relation = api.Relation.create(schema='sushi', identifier='waiters') %}"
        "{{ relation.schema }} {{ relation.identifier}}"
    )

    assert context.render(jinja) == "sushi waiters"


@pytest.mark.xdist_group("dbt_manifest")
def test_column(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ api.Column }}") == "<class 'dbt.adapters.base.column.Column'>"

    jinja = (
        "{% set col = api.Column('foo', 'integer') %}{{ col.is_integer() }} {{ col.is_string()}}"
    )

    assert context.render(jinja) == "True False"


@pytest.mark.xdist_group("dbt_manifest")
def test_quote(sushi_test_project: Project):
    context = sushi_test_project.context

    jinja = "{{ adapter.quote('foo') }} {{ adapter.quote('bar') }}"
    assert context.render(jinja) == '"foo" "bar"'


@pytest.mark.xdist_group("dbt_manifest")
def test_as_filters(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ True | as_bool }}") == "True"
    assert context.render("{{ 'valid' | as_bool }}") == "valid"

    assert context.render("{{ 123 | as_number }}") == "123"
    assert context.render("{{ 'valid' | as_number }}") == "valid"

    assert context.render("{{ None | as_text }}") == ""

    assert context.render("{{ None | as_native }}") == "None"


@pytest.mark.xdist_group("dbt_manifest")
def test_set(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ set([1, 1, 2]) }}") == "{1, 2}"
    assert context.render("{{ set(1) }}") == "None"

    assert context.render("{{ set_strict([1, 1, 2]) }}") == "{1, 2}"
    with pytest.raises(TypeError):
        assert context.render("{{ set_strict(1) }}")


@pytest.mark.xdist_group("dbt_manifest")
def test_json(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ tojson({'key': 'value'}) }}") == """{"key": "value"}"""
    assert context.render("{{ tojson(set([1])) }}") == "None"

    assert context.render("""{{ fromjson('{"key": "value"}') }}""") == "{'key': 'value'}"
    assert context.render("""{{ fromjson('invalid') }}""") == "None"


@pytest.mark.xdist_group("dbt_manifest")
def test_yaml(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ toyaml({'key': 'value'}) }}").strip() == "key: value"
    assert context.render("{{ toyaml(invalid) }}", invalid=lambda: "") == "None"

    assert context.render("""{{ fromyaml('key: value') }}""") == "{'key': 'value'}"


@pytest.mark.xdist_group("dbt_manifest")
def test_zip(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ zip([1, 2], ['a', 'b']) }}") == "[(1, 'a'), (2, 'b')]"
    assert context.render("{{ zip(12, ['a', 'b']) }}") == "None"

    assert context.render("{{ zip_strict([1, 2], ['a', 'b']) }}") == "[(1, 'a'), (2, 'b')]"
    with pytest.raises(TypeError):
        context.render("{{ zip_strict(12, ['a', 'b']) }}")


@pytest.mark.xdist_group("dbt_manifest")
def test_dbt_version(sushi_test_project: Project):
    context = sushi_test_project.context

    assert context.render("{{ dbt_version }}").startswith("1.")


@pytest.mark.xdist_group("dbt_manifest")
def test_dbt_on_run_start_end(sushi_test_project: Project):
    # Validate perservation of dbt's order of execution
    assert sushi_test_project.packages["sushi"].on_run_start["sushi-on-run-start-0"].index == 0
    assert sushi_test_project.packages["sushi"].on_run_start["sushi-on-run-start-1"].index == 1
    assert sushi_test_project.packages["sushi"].on_run_end["sushi-on-run-end-0"].index == 0
    assert sushi_test_project.packages["sushi"].on_run_end["sushi-on-run-end-1"].index == 1
    assert (
        sushi_test_project.packages["customers"].on_run_start["customers-on-run-start-0"].index == 0
    )
    assert (
        sushi_test_project.packages["customers"].on_run_start["customers-on-run-start-1"].index == 1
    )
    assert sushi_test_project.packages["customers"].on_run_end["customers-on-run-end-0"].index == 0
    assert sushi_test_project.packages["customers"].on_run_end["customers-on-run-end-1"].index == 1

    assert (
        sushi_test_project.packages["customers"].on_run_start["customers-on-run-start-0"].sql
        == "CREATE TABLE IF NOT EXISTS to_be_executed_first (col VARCHAR);"
    )
    assert (
        sushi_test_project.packages["customers"].on_run_start["customers-on-run-start-1"].sql
        == "CREATE TABLE IF NOT EXISTS analytic_stats_packaged_project (physical_table VARCHAR, evaluation_time VARCHAR);"
    )
    assert (
        sushi_test_project.packages["customers"].on_run_end["customers-on-run-end-1"].sql
        == "{{ packaged_tables(schemas) }}"
    )

    assert (
        sushi_test_project.packages["sushi"].on_run_start["sushi-on-run-start-0"].sql
        == "CREATE TABLE IF NOT EXISTS analytic_stats (physical_table VARCHAR, evaluation_time VARCHAR);"
    )
    assert (
        sushi_test_project.packages["sushi"].on_run_end["sushi-on-run-end-0"].sql
        == "{{ create_tables(schemas) }}"
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_parsetime_adapter_call(
    assert_exp_eq, sushi_test_project: Project, sushi_test_dbt_context: Context
):
    model_config = ModelConfig(
        name="model",
        package_name="package",
        alias="test",
        schema="sushi",
        sql="""
            {% set results = run_query('select 1 as one') %}
            SELECT {{ results.columns[0].values()[0] }} AS one FROM {{ this.identifier }}
        """,
    )
    context = sushi_test_project.context

    sqlmesh_model = model_config.to_sqlmesh(context)
    assert sqlmesh_model.render_query() is None
    assert sqlmesh_model.columns_to_types is None
    assert not sqlmesh_model.annotated
    with pytest.raises(SQLMeshError):
        sqlmesh_model.ctas_query()

    engine_adapter = sushi_test_dbt_context.engine_adapter
    assert_exp_eq(
        sqlmesh_model.render_query_or_raise(engine_adapter=engine_adapter).sql(),
        'SELECT 1 AS "one" FROM "test" AS "test"',
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_partition_by(sushi_test_project: Project, caplog):
    context = sushi_test_project.context
    context.target = BigQueryConfig(name="production", database="main", schema="sushi")
    model_config = ModelConfig(
        name="model",
        alias="model",
        schema="test",
        package_name="package",
        materialized="table",
        unique_key="ds",
        partition_by={"field": "ds", "granularity": "month"},
        sql="""SELECT 1 AS one, ds FROM foo""",
    )
    date_trunc_expr = model_config.to_sqlmesh(context).partitioned_by[0]
    assert date_trunc_expr.sql(dialect="bigquery") == "DATE_TRUNC(`ds`, MONTH)"
    assert date_trunc_expr.sql() == "DATE_TRUNC('MONTH', \"ds\")"

    model_config.partition_by = {"field": "`ds`", "data_type": "datetime", "granularity": "day"}
    datetime_trunc_expr = model_config.to_sqlmesh(context).partitioned_by[0]
    assert datetime_trunc_expr.sql(dialect="bigquery") == "datetime_trunc(`ds`, DAY)"
    assert datetime_trunc_expr.sql() == 'DATETIME_TRUNC("ds", DAY)'

    model_config.partition_by = {"field": "ds", "data_type": "timestamp", "granularity": "day"}
    timestamp_trunc_expr = model_config.to_sqlmesh(context).partitioned_by[0]
    assert timestamp_trunc_expr.sql(dialect="bigquery") == "timestamp_trunc(`ds`, DAY)"
    assert timestamp_trunc_expr.sql() == 'TIMESTAMP_TRUNC("ds", DAY)'

    model_config.partition_by = {
        "field": "one",
        "data_type": "int64",
        "range": {"start": 0, "end": 10, "interval": 2},
    }
    assert (
        model_config.to_sqlmesh(context).partitioned_by[0].sql()
        == 'RANGE_BUCKET("one", GENERATE_SERIES(0, 10, 2))'
    )

    model_config.partition_by = {"field": "ds", "data_type": "date", "granularity": "day"}
    assert model_config.to_sqlmesh(context).partitioned_by == [exp.to_column("ds", quoted=True)]

    context.target = DuckDbConfig(name="target", schema="foo")
    assert model_config.to_sqlmesh(context).partitioned_by == []

    context.target = SnowflakeConfig(
        name="target", schema="test", database="test", account="foo", user="bar", password="baz"
    )
    assert model_config.to_sqlmesh(context).partitioned_by == []
    assert (
        "Ignoring partition_by config for model 'model' targeting snowflake. The partition_by config is not supported for Snowflake."
        in caplog.text
    )

    model_config = ModelConfig(
        name="model",
        alias="model",
        schema="test",
        package_name="package",
        materialized=Materialization.VIEW.value,
        unique_key="ds",
        partition_by={"field": "ds", "granularity": "month"},
        sql="""SELECT 1 AS one, ds FROM foo""",
    )
    assert model_config.to_sqlmesh(context).partitioned_by == []

    model_config = ModelConfig(
        name="model",
        alias="model",
        schema="test",
        package_name="package",
        materialized=Materialization.EPHEMERAL.value,
        unique_key="ds",
        partition_by={"field": "ds", "granularity": "month"},
        sql="""SELECT 1 AS one, ds FROM foo""",
    )
    assert model_config.to_sqlmesh(context).partitioned_by == []

    with pytest.raises(ConfigError, match="Unexpected data_type 'string' in partition_by"):
        ModelConfig(
            name="model",
            alias="model",
            schema="test",
            package_name="package",
            materialized="table",
            partition_by={"field": "ds", "data_type": "string"},
            sql="""SELECT 1 AS one, ds FROM foo""",
        )


@pytest.mark.xdist_group("dbt_manifest")
def test_partition_by_none(sushi_test_project: Project):
    context = sushi_test_project.context
    context.target = BigQueryConfig(name="production", database="main", schema="sushi")
    model_config = ModelConfig(
        name="model",
        alias="model",
        schema="test",
        package_name="package",
        materialized="table",
        unique_key="ds",
        partition_by=None,
        sql="""SELECT 1 AS one, ds FROM foo""",
    )
    assert model_config.partition_by is None


@pytest.mark.xdist_group("dbt_manifest")
def test_relation_info_to_relation():
    assert _relation_info_to_relation(
        {"quote_policy": {}},
        BaseRelation,
        Policy(database=True, schema=True, identifier=True),
    ).quote_policy == Policy(database=True, schema=True, identifier=True)

    assert _relation_info_to_relation(
        {"quote_policy": {"database": None, "schema": None, "identifier": None}},
        BaseRelation,
        Policy(database=True, schema=True, identifier=True),
    ).quote_policy == Policy(database=True, schema=True, identifier=True)

    assert _relation_info_to_relation(
        {"quote_policy": {"database": False, "schema": None, "identifier": None}},
        BaseRelation,
        Policy(database=True, schema=True, identifier=True),
    ).quote_policy == Policy(database=False, schema=True, identifier=True)

    assert _relation_info_to_relation(
        {"quote_policy": {"database": False}},
        BaseRelation,
        Policy(database=True, schema=True, identifier=True),
    ).quote_policy == Policy(database=False, schema=True, identifier=True)

    assert _relation_info_to_relation(
        {"quote_policy": {"database": False, "schema": False, "identifier": False}},
        BaseRelation,
        Policy(database=True, schema=True, identifier=True),
    ).quote_policy == Policy(database=False, schema=False, identifier=False)


@pytest.mark.xdist_group("dbt_manifest")
def test_is_incremental(sushi_test_project: Project, assert_exp_eq, mocker):
    model_config = ModelConfig(
        name="model",
        package_name="package",
        schema="sushi",
        alias="some_table",
        sql="""
        SELECT 1 AS one FROM tbl_a
        {% if is_incremental() %}
        WHERE ds > (SELECT MAX(ds) FROM model)
        {% endif %}
        """,
    )
    context = sushi_test_project.context

    assert_exp_eq(
        model_config.to_sqlmesh(context).render_query_or_raise().sql(),
        'SELECT 1 AS "one" FROM "tbl_a" AS "tbl_a"',
    )

    snapshot = mocker.Mock()
    snapshot.intervals = [1]
    snapshot.is_incremental = True

    assert_exp_eq(
        model_config.to_sqlmesh(context).render_query_or_raise(snapshot=snapshot).sql(),
        'SELECT 1 AS "one" FROM "tbl_a" AS "tbl_a" WHERE "ds" > (SELECT MAX("ds") FROM "model" AS "model")',
    )

    # If the snapshot_table_exists flag was set to False, intervals should be ignored
    assert_exp_eq(
        model_config.to_sqlmesh(context)
        .render_query_or_raise(snapshot=snapshot, snapshot_table_exists=False)
        .sql(),
        'SELECT 1 AS "one" FROM "tbl_a" AS "tbl_a"',
    )

    # If the snapshot_table_exists flag was set to True, intervals should be taken into account
    assert_exp_eq(
        model_config.to_sqlmesh(context)
        .render_query_or_raise(snapshot=snapshot, snapshot_table_exists=True)
        .sql(),
        'SELECT 1 AS "one" FROM "tbl_a" AS "tbl_a" WHERE "ds" > (SELECT MAX("ds") FROM "model" AS "model")',
    )
    snapshot.intervals = []
    assert_exp_eq(
        model_config.to_sqlmesh(context)
        .render_query_or_raise(snaspshot=snapshot, snapshot_table_exists=True)
        .sql(),
        'SELECT 1 AS "one" FROM "tbl_a" AS "tbl_a"',
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_is_incremental_non_incremental_model(sushi_test_project: Project, assert_exp_eq, mocker):
    model_config = ModelConfig(
        name="model",
        package_name="package",
        schema="sushi",
        alias="some_table",
        sql="""
        SELECT 1 AS one FROM tbl_a
        {% if is_incremental() %}
        WHERE ds > (SELECT MAX(ds) FROM model)
        {% endif %}
        """,
    )
    context = sushi_test_project.context

    snapshot = mocker.Mock()
    snapshot.intervals = [1]
    snapshot.is_incremental = False

    assert_exp_eq(
        model_config.to_sqlmesh(context).render_query_or_raise(snapshot=snapshot).sql(),
        'SELECT 1 AS "one" FROM "tbl_a" AS "tbl_a"',
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_dbt_max_partition(sushi_test_project: Project, assert_exp_eq, mocker: MockerFixture):
    model_config = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        schema="sushi",
        partition_by={"field": "`ds`", "data_type": "datetime", "granularity": "month"},
        materialized=Materialization.INCREMENTAL,
        sql="""
        SELECT 1 AS one FROM tbl_a
        {% if is_incremental() %}
        WHERE ds > _dbt_max_partition
        {% endif %}
        """,
    )
    context = sushi_test_project.context
    context.target = BigQueryConfig(
        name="test_target", schema="test_schema", database="test-project"
    )

    pre_statement = model_config.to_sqlmesh(context).pre_statements[-1]  # type: ignore

    assert (
        pre_statement.sql().strip()
        == """
JINJA_STATEMENT_BEGIN;
{% if is_incremental() %}
  DECLARE _dbt_max_partition DATETIME DEFAULT (
    COALESCE((SELECT MAX(PARSE_DATETIME('%Y%m', partition_id)) FROM `{{ target.database }}`.`{{ adapter.resolve_schema(this) }}`.`INFORMATION_SCHEMA.PARTITIONS` AS PARTITIONS WHERE table_name = '{{ adapter.resolve_identifier(this) }}' AND NOT partition_id IS NULL AND partition_id <> '__NULL__'), CAST('1970-01-01' AS DATETIME))
  );
{% endif %}
JINJA_END;""".strip()
    )

    assert d.parse_one(pre_statement.sql()) == pre_statement


@pytest.mark.xdist_group("dbt_manifest")
def test_bigquery_physical_properties(sushi_test_project: Project, mocker: MockerFixture):
    context = sushi_test_project.context
    context.target = BigQueryConfig(
        name="test_target", schema="test_schema", database="test-project"
    )

    base_config = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        schema="sushi",
        partition_by={"field": "`ds`", "data_type": "datetime", "granularity": "month"},
        materialized=Materialization.INCREMENTAL,
        sql="SELECT 1 AS one FROM tbl_a",
    )

    assert base_config.to_sqlmesh(context).physical_properties == {}

    assert base_config.copy(
        update={"require_partition_filter": True, "partition_expiration_days": 7}
    ).to_sqlmesh(context).physical_properties == {
        "require_partition_filter": exp.convert(True),
        "partition_expiration_days": exp.convert(7),
    }

    assert base_config.copy(update={"require_partition_filter": True}).to_sqlmesh(
        context
    ).physical_properties == {
        "require_partition_filter": exp.convert(True),
    }

    assert base_config.copy(update={"partition_expiration_days": 7}).to_sqlmesh(
        context
    ).physical_properties == {
        "partition_expiration_days": exp.convert(7),
    }


@pytest.mark.xdist_group("dbt_manifest")
def test_clickhouse_properties(mocker: MockerFixture):
    context = DbtContext(target_name="production")
    context._project_name = "Foo"
    context._target = ClickhouseConfig(name="production")
    model_config = ModelConfig(
        name="model",
        alias="model",
        schema="test",
        package_name="package",
        materialized="incremental",
        incremental_strategy="delete+insert",
        incremental_predicates=["ds > (SELECT MAX(ds) FROM model)"],
        query_settings={"QUERY_SETTING": "value"},
        sharding_key="rand()",
        engine="MergeTree()",
        partition_by=["toMonday(ds)", "partition_col"],
        order_by=["toStartOfWeek(ds)", "order_col"],
        primary_key=["ds", "primary_key_col"],
        ttl="time + INTERVAL 1 WEEK",
        settings={"SETTING": "value"},
        sql="""SELECT 1 AS one, ds FROM foo""",
    )

    with patch.object(get_console(), "log_warning") as mock_logger:
        model_to_sqlmesh = model_config.to_sqlmesh(context)

    assert [call[0][0] for call in mock_logger.call_args_list] == [
        "The 'delete+insert' incremental strategy is not supported - SQLMesh will use the temp table/partition swap strategy.",
        "SQLMesh does not support 'incremental_predicates' - they will not be applied.",
        "SQLMesh does not support the 'query_settings' model configuration parameter. Specify the query settings directly in the model query.",
        "SQLMesh does not support the 'sharding_key' model configuration parameter or distributed materializations.",
    ]

    assert [e.sql("clickhouse") for e in model_to_sqlmesh.partitioned_by] == [
        "dateTrunc('WEEK', \"ds\")",
        '"partition_col"',
    ]
    assert model_to_sqlmesh.storage_format == "MergeTree()"

    physical_properties = model_to_sqlmesh.physical_properties
    assert [e.sql("clickhouse", identify=True) for e in physical_properties["order_by"]] == [
        'toStartOfWeek("ds")',
        '"order_col"',
    ]
    assert [e.sql("clickhouse", identify=True) for e in physical_properties["primary_key"]] == [
        '"ds"',
        '"primary_key_col"',
    ]
    assert physical_properties["ttl"].sql("clickhouse") == "time + INTERVAL 1 WEEK"
    assert physical_properties["SETTING"].sql("clickhouse") == "value"


@pytest.mark.xdist_group("dbt_manifest")
def test_snapshot_json_payload():
    sushi_context = Context(paths=["tests/fixtures/dbt/sushi_test"])
    snapshot_json = json.loads(
        _snapshot_to_json(sushi_context.get_snapshot("sushi.top_waiters", raise_if_missing=True))
    )
    assert snapshot_json["node"]["jinja_macros"]["global_objs"]["target"] == {
        "type": "duckdb",
        "name": "in_memory",
        "database": "memory",
        "schema": "sushi",
        "threads": 1,
        "target_name": "in_memory",
    }


@pytest.mark.xdist_group("dbt_manifest")
@time_machine.travel("2023-01-08 00:00:00 UTC")
def test_dbt_package_macros(sushi_test_project: Project):
    context = sushi_test_project.context

    # Make sure external macros are available.
    assert context.render("{{ dbt.current_timestamp() }}") == "now()"
    # Make sure builtins are available too.
    assert context.render("{{ dbt.run_started_at }}") == "2023-01-08 00:00:00+00:00"


@pytest.mark.xdist_group("dbt_manifest")
def test_dbt_vars(sushi_test_project: Project):
    context = sushi_test_project.context
    context.set_and_render_variables(
        sushi_test_project.packages["customers"].variables, "customers"
    )

    assert context.render("{{ var('some_other_var') }}") == "5"
    assert context.render("{{ var('some_other_var', 0) }}") == "5"
    assert context.render("{{ var('missing') }}") == "None"
    assert context.render("{{ var('missing', 0) }}") == "0"

    assert context.render("{{ var.has_var('some_other_var') }}") == "True"
    assert context.render("{{ var.has_var('missing') }}") == "False"


@pytest.mark.xdist_group("dbt_manifest")
def test_snowflake_session_properties(sushi_test_project: Project, mocker: MockerFixture):
    context = sushi_test_project.context
    context.target = SnowflakeConfig(
        name="target", schema="test", database="test", account="foo", user="bar", password="baz"
    )

    base_config = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        schema="sushi",
        partition_by={"field": "`ds`", "data_type": "datetime", "granularity": "month"},
        materialized=Materialization.INCREMENTAL,
        sql="SELECT 1 AS one FROM tbl_a",
    )

    assert base_config.to_sqlmesh(context).session_properties == {}

    model_with_warehouse = base_config.copy(
        update={"snowflake_warehouse": "test_warehouse"}
    ).to_sqlmesh(context)

    assert model_with_warehouse.session_properties_ == exp.Tuple(
        expressions=[exp.Literal.string("warehouse").eq(exp.Literal.string("test_warehouse"))]
    )
    assert model_with_warehouse.session_properties == {"warehouse": "test_warehouse"}


def test_model_cluster_by():
    context = DbtContext()
    context._target = SnowflakeConfig(
        name="target",
        schema="test",
        database="test",
        account="account",
        user="user",
        password="password",
    )

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        cluster_by="Bar",
        sql="SELECT * FROM baz",
        materialized=Materialization.TABLE.value,
    )
    assert model.to_sqlmesh(context).clustered_by == [exp.to_column('"BAR"')]

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        cluster_by=["Bar", "qux"],
        sql="SELECT * FROM baz",
        materialized=Materialization.TABLE.value,
    )
    assert model.to_sqlmesh(context).clustered_by == [
        exp.to_column('"BAR"'),
        exp.to_column('"QUX"'),
    ]

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        cluster_by=["Bar", "qux"],
        sql="SELECT * FROM baz",
        materialized=Materialization.VIEW.value,
    )
    assert model.to_sqlmesh(context).clustered_by == []

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        cluster_by=["Bar", "qux"],
        sql="SELECT * FROM baz",
        materialized=Materialization.EPHEMERAL.value,
    )
    assert model.to_sqlmesh(context).clustered_by == []

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        cluster_by="Bar, qux",
        sql="SELECT * FROM baz",
        materialized=Materialization.TABLE.value,
    )
    assert model.to_sqlmesh(context).clustered_by == [
        exp.to_column('"BAR"'),
        exp.to_column('"QUX"'),
    ]

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        cluster_by=['"Bar,qux"'],
        sql="SELECT * FROM baz",
        materialized=Materialization.TABLE.value,
    )
    assert model.to_sqlmesh(context).clustered_by == [
        exp.to_column('"Bar,qux"'),
    ]

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        cluster_by='"Bar,qux"',
        sql="SELECT * FROM baz",
        materialized=Materialization.TABLE.value,
    )
    assert model.to_sqlmesh(context).clustered_by == [
        exp.to_column('"Bar,qux"'),
    ]

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        cluster_by=["to_date(Bar),qux"],
        sql="SELECT * FROM baz",
        materialized=Materialization.TABLE.value,
    )
    assert model.to_sqlmesh(context).clustered_by == [
        exp.TsOrDsToDate(this=exp.to_column('"BAR"')),
        exp.to_column('"QUX"'),
    ]


def test_snowflake_dynamic_table():
    context = DbtContext()
    context._target = SnowflakeConfig(
        name="target",
        schema="test",
        database="test",
        account="account",
        user="user",
        password="password",
    )

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        sql="SELECT * FROM baz",
        materialized=Materialization.DYNAMIC_TABLE.value,
        target_lag="1 hour",
        snowflake_warehouse="SMALL",
    )

    as_sqlmesh = model.to_sqlmesh(context)
    assert as_sqlmesh.kind == ManagedKind()
    assert as_sqlmesh.physical_properties == {
        "target_lag": exp.Literal.string("1 hour"),
        "warehouse": exp.Literal.string("SMALL"),
    }

    # both target_lag and snowflake_warehouse are required properties
    # https://docs.getdbt.com/reference/resource-configs/snowflake-configs#dynamic-tables
    for required_property in ["target_lag", "snowflake_warehouse"]:
        with pytest.raises(ConfigError, match=r".*must be set for dynamic tables"):
            model.copy(update={required_property: None}).to_sqlmesh(context)


@pytest.mark.xdist_group("dbt_manifest")
def test_refs_in_jinja_globals(sushi_test_project: Project, mocker: MockerFixture):
    context = sushi_test_project.context

    sqlmesh_model = t.cast(
        SqlModel,
        sushi_test_project.packages["sushi"].models["simple_model_b"].to_sqlmesh(context),
    )
    assert set(sqlmesh_model.jinja_macros.global_objs["refs"].keys()) == {"simple_model_a"}  # type: ignore

    sqlmesh_model = t.cast(
        SqlModel,
        sushi_test_project.packages["sushi"].models["top_waiters"].to_sqlmesh(context),
    )
    assert set(sqlmesh_model.jinja_macros.global_objs["refs"].keys()) == {  # type: ignore
        "waiter_revenue_by_day",
        "sushi.waiter_revenue_by_day",
    }


def test_allow_partials_by_default():
    context = DbtContext()
    context._target = SnowflakeConfig(
        name="target",
        schema="test",
        database="test",
        account="account",
        user="user",
        password="password",
    )

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        sql="SELECT * FROM baz",
        materialized=Materialization.TABLE.value,
    )
    assert model.allow_partials
    assert model.to_sqlmesh(context).allow_partials

    model.materialized = Materialization.INCREMENTAL.value
    assert model.allow_partials
    assert model.to_sqlmesh(context).allow_partials

    model.allow_partials = True
    assert model.to_sqlmesh(context).allow_partials

    model.allow_partials = False
    assert not model.to_sqlmesh(context).allow_partials


def test_grain():
    context = DbtContext()
    context._target = SnowflakeConfig(
        name="target",
        schema="test",
        database="test",
        account="account",
        user="user",
        password="password",
    )

    model = ModelConfig(
        name="model",
        alias="model",
        package_name="package",
        target_schema="test",
        sql="SELECT * FROM baz",
        materialized=Materialization.TABLE.value,
        grain=["id_a", "id_b"],
    )
    assert model.to_sqlmesh(context).grains == [exp.to_column("id_a"), exp.to_column("id_b")]

    model.grain = "id_a"
    assert model.to_sqlmesh(context).grains == [exp.to_column("id_a")]


@pytest.mark.xdist_group("dbt_manifest")
def test_on_run_start_end():
    sushi_context = Context(paths=["tests/fixtures/dbt/sushi_test"])
    assert len(sushi_context._environment_statements) == 2

    # Root project's on run start / on run end should be first by checking the macros
    root_environment_statements = sushi_context._environment_statements[0]
    assert "create_tables" in root_environment_statements.jinja_macros.root_macros

    # Validate order of execution to be correct
    assert root_environment_statements.before_all == [
        "JINJA_STATEMENT_BEGIN;\nCREATE TABLE IF NOT EXISTS analytic_stats (physical_table VARCHAR, evaluation_time VARCHAR);\nJINJA_END;",
        "JINJA_STATEMENT_BEGIN;\nCREATE TABLE IF NOT EXISTS to_be_executed_last (col VARCHAR);\nJINJA_END;",
        """JINJA_STATEMENT_BEGIN;\nSELECT {{ var("yet_another_var") }} AS var, '{{ source("raw", "items").identifier }}' AS src, '{{ ref("waiters").identifier }}' AS ref;\nJINJA_END;""",
        "JINJA_STATEMENT_BEGIN;\n{{ log_value('on-run-start') }}\nJINJA_END;",
    ]
    assert root_environment_statements.after_all == [
        "JINJA_STATEMENT_BEGIN;\n{{ create_tables(schemas) }}\nJINJA_END;",
        "JINJA_STATEMENT_BEGIN;\nDROP TABLE to_be_executed_last;\nJINJA_END;",
        "JINJA_STATEMENT_BEGIN;\n{{ graph_usage() }}\nJINJA_END;",
    ]

    assert root_environment_statements.jinja_macros.root_package_name == "sushi"

    rendered_before_all = render_statements(
        root_environment_statements.before_all,
        dialect=sushi_context.default_dialect,
        python_env=root_environment_statements.python_env,
        jinja_macros=root_environment_statements.jinja_macros,
        runtime_stage=RuntimeStage.BEFORE_ALL,
    )

    runtime_rendered_after_all = render_statements(
        root_environment_statements.after_all,
        dialect=sushi_context.default_dialect,
        python_env=root_environment_statements.python_env,
        jinja_macros=root_environment_statements.jinja_macros,
        snapshots=sushi_context.snapshots,
        runtime_stage=RuntimeStage.AFTER_ALL,
        environment_naming_info=EnvironmentNamingInfo(name="dev"),
        engine_adapter=sushi_context.engine_adapter,
    )

    # not passing engine adapter simulates "parse-time" rendering
    parse_time_rendered_after_all = render_statements(
        root_environment_statements.after_all,
        dialect=sushi_context.default_dialect,
        python_env=root_environment_statements.python_env,
        jinja_macros=root_environment_statements.jinja_macros,
        snapshots=sushi_context.snapshots,
        runtime_stage=RuntimeStage.AFTER_ALL,
        environment_naming_info=EnvironmentNamingInfo(name="dev"),
    )

    # validate that the graph_table statement is the same between parse-time and runtime rendering
    assert sorted(parse_time_rendered_after_all) == sorted(runtime_rendered_after_all)
    graph_table_stmt = runtime_rendered_after_all[-1]
    assert graph_table_stmt == parse_time_rendered_after_all[-1]

    assert rendered_before_all == [
        "CREATE TABLE IF NOT EXISTS analytic_stats (physical_table TEXT, evaluation_time TEXT)",
        "CREATE TABLE IF NOT EXISTS to_be_executed_last (col TEXT)",
        "SELECT 1 AS var, 'items' AS src, 'waiters' AS ref",
    ]

    # The jinja macro should have resolved the schemas for this environment and generated corresponding statements
    expected_statements = [
        "CREATE OR REPLACE TABLE schema_table_snapshots__dev AS SELECT 'snapshots__dev' AS schema",
        "CREATE OR REPLACE TABLE schema_table_sushi__dev AS SELECT 'sushi__dev' AS schema",
        "DROP TABLE to_be_executed_last",
    ]
    assert sorted(runtime_rendered_after_all[:-1]) == sorted(expected_statements)

    # Assert the models with their materialisations are present in the rendered graph_table statement
    assert "'model.sushi.simple_model_a' AS unique_id, 'table' AS materialized" in graph_table_stmt
    assert "'model.sushi.waiters' AS unique_id, 'ephemeral' AS materialized" in graph_table_stmt
    assert "'model.sushi.simple_model_b' AS unique_id, 'table' AS materialized" in graph_table_stmt
    assert (
        "'model.sushi.waiter_as_customer_by_day' AS unique_id, 'incremental' AS materialized"
        in graph_table_stmt
    )
    assert "'model.sushi.top_waiters' AS unique_id, 'view' AS materialized" in graph_table_stmt
    assert "'model.customers.customers' AS unique_id, 'view' AS materialized" in graph_table_stmt
    assert (
        "'model.customers.customer_revenue_by_day' AS unique_id, 'incremental' AS materialized"
        in graph_table_stmt
    )
    assert (
        "'model.sushi.waiter_revenue_by_day.v1' AS unique_id, 'incremental' AS materialized"
        in graph_table_stmt
    )
    assert (
        "'model.sushi.waiter_revenue_by_day.v2' AS unique_id, 'incremental' AS materialized"
        in graph_table_stmt
    )

    # Nested dbt_packages on run start / on run end
    packaged_environment_statements = sushi_context._environment_statements[1]

    # Validate order of execution to be correct
    assert packaged_environment_statements.before_all == [
        "JINJA_STATEMENT_BEGIN;\nCREATE TABLE IF NOT EXISTS to_be_executed_first (col VARCHAR);\nJINJA_END;",
        "JINJA_STATEMENT_BEGIN;\nCREATE TABLE IF NOT EXISTS analytic_stats_packaged_project (physical_table VARCHAR, evaluation_time VARCHAR);\nJINJA_END;",
    ]
    assert packaged_environment_statements.after_all == [
        "JINJA_STATEMENT_BEGIN;\nDROP TABLE to_be_executed_first\nJINJA_END;",
        "JINJA_STATEMENT_BEGIN;\n{{ packaged_tables(schemas) }}\nJINJA_END;",
    ]

    assert "packaged_tables" in packaged_environment_statements.jinja_macros.root_macros
    assert packaged_environment_statements.jinja_macros.root_package_name == "sushi"

    rendered_before_all = render_statements(
        packaged_environment_statements.before_all,
        dialect=sushi_context.default_dialect,
        python_env=packaged_environment_statements.python_env,
        jinja_macros=packaged_environment_statements.jinja_macros,
        runtime_stage=RuntimeStage.BEFORE_ALL,
    )

    rendered_after_all = render_statements(
        packaged_environment_statements.after_all,
        dialect=sushi_context.default_dialect,
        python_env=packaged_environment_statements.python_env,
        jinja_macros=packaged_environment_statements.jinja_macros,
        snapshots=sushi_context.snapshots,
        runtime_stage=RuntimeStage.AFTER_ALL,
        environment_naming_info=EnvironmentNamingInfo(name="dev"),
        engine_adapter=sushi_context.engine_adapter,
    )

    # Validate order of execution to match dbt's
    assert rendered_before_all == [
        "CREATE TABLE IF NOT EXISTS to_be_executed_first (col TEXT)",
        "CREATE TABLE IF NOT EXISTS analytic_stats_packaged_project (physical_table TEXT, evaluation_time TEXT)",
    ]

    # This on run end statement should be executed first
    assert rendered_after_all[0] == "DROP TABLE to_be_executed_first"

    # The table names is an indication of the rendering of the dbt_packages statements
    assert sorted(rendered_after_all) == sorted(
        [
            "DROP TABLE to_be_executed_first",
            "CREATE OR REPLACE TABLE schema_table_snapshots__dev_nested_package AS SELECT 'snapshots__dev' AS schema",
            "CREATE OR REPLACE TABLE schema_table_sushi__dev_nested_package AS SELECT 'sushi__dev' AS schema",
        ]
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_dynamic_var_names(sushi_test_project: Project, sushi_test_dbt_context: Context):
    context = sushi_test_project.context
    context.set_and_render_variables(sushi_test_project.packages["sushi"].variables, "sushi")
    context.target = BigQueryConfig(name="production", database="main", schema="sushi")
    model_config = ModelConfig(
        name="model",
        alias="model",
        schema="test",
        package_name="package",
        materialized="table",
        unique_key="ds",
        partition_by={"field": "ds", "granularity": "month"},
        sql="""
        {% set var_name = "yet_" + "another_" + "var" %}
        {% set results = run_query('select 1 as one') %}
        {% if results %}
        SELECT {{ results.columns[0].values()[0] }} AS one {{ var(var_name) }} AS var FROM {{ this.identifier }}
        {% else %}
        SELECT NULL AS one {{ var(var_name) }} AS var FROM {{ this.identifier }}
        {% endif %}
        """,
        dependencies=Dependencies(has_dynamic_var_names=True),
    )
    converted_model = model_config.to_sqlmesh(context)
    assert "yet_another_var" in converted_model.jinja_macros.global_objs["vars"]  # type: ignore

    # Test the existing model in the sushi project
    assert (
        "dynamic_test_var"  # type: ignore
        in sushi_test_dbt_context.get_model(
            "sushi.waiter_revenue_by_day_v2"
        ).jinja_macros.global_objs["vars"]
    )


@pytest.mark.xdist_group("dbt_manifest")
def test_dynamic_var_names_in_macro(sushi_test_project: Project):
    context = sushi_test_project.context
    context.set_and_render_variables(sushi_test_project.packages["sushi"].variables, "sushi")
    context.target = BigQueryConfig(name="production", database="main", schema="sushi")
    model_config = ModelConfig(
        name="model",
        alias="model",
        schema="test",
        package_name="package",
        materialized="table",
        unique_key="ds",
        partition_by={"field": "ds", "granularity": "month"},
        sql="""
        {% set var_name = "dynamic_" + "test_" + "var" %}
        SELECT {{ sushi.dynamic_var_name_dependency(var_name) }} AS var
        """,
        dependencies=Dependencies(
            macros=[MacroReference(package="sushi", name="dynamic_var_name_dependency")],
            has_dynamic_var_names=True,
        ),
    )
    converted_model = model_config.to_sqlmesh(context)
    assert "dynamic_test_var" in converted_model.jinja_macros.global_objs["vars"]  # type: ignore


def test_selected_resources_with_selectors():
    sushi_context = Context(paths=["tests/fixtures/dbt/sushi_test"])

    # A plan with a specific model selection
    plan_builder = sushi_context.plan_builder(select_models=["sushi.customers"])
    plan = plan_builder.build()
    assert len(plan.selected_models) == 1
    selected_model = list(plan.selected_models)[0]
    assert "customers" in selected_model

    # Plan without model selections should include all models
    plan_builder = sushi_context.plan_builder()
    plan = plan_builder.build()
    assert plan.selected_models is not None
    assert len(plan.selected_models) > 10

    # with downstream models should select customers and at least one downstream model
    plan_builder = sushi_context.plan_builder(select_models=["sushi.customers+"])
    plan = plan_builder.build()
    assert plan.selected_models is not None
    assert len(plan.selected_models) >= 2
    assert any("customers" in model for model in plan.selected_models)

    # Test wildcard selection
    plan_builder = sushi_context.plan_builder(select_models=["sushi.waiter_*"])
    plan = plan_builder.build()
    assert plan.selected_models is not None
    assert len(plan.selected_models) >= 4
    assert all("waiter" in model for model in plan.selected_models)


@pytest.mark.xdist_group("dbt_manifest")
def test_selected_resources_context_variable(
    sushi_test_project: Project, sushi_test_dbt_context: Context
):
    context = sushi_test_project.context

    # empty selected resources
    direct_access = context.render("{{ selected_resources }}")
    assert direct_access == "[]"

    # selected_resources is iterable and count items
    test_jinja = """
    {%- set resources = [] -%}
    {%- for resource in selected_resources -%}
        {%- do resources.append(resource) -%}
    {%- endfor -%}
    {{ resources | length }}
    """
    result = context.render(test_jinja)
    assert result.strip() == "0"

    # selected_resources in conditions
    test_condition = """
    {%- if selected_resources -%}
        has_resources
    {%- else -%}
        no_resources
    {%- endif -%}
    """
    result = context.render(test_condition)
    assert result.strip() == "no_resources"

    #  selected resources in dbt format
    selected_resources = [
        "model.jaffle_shop.customers",
        "model.jaffle_shop.items",
        "model.jaffle_shop.orders",
    ]

    # check the jinja macros rendering
    result = context.render("{{ selected_resources }}", selected_resources=selected_resources)
    assert result == selected_resources.__repr__()

    result = context.render(test_jinja, selected_resources=selected_resources)
    assert result.strip() == "3"

    result = context.render(test_condition, selected_resources=selected_resources)
    assert result.strip() == "has_resources"


def test_ignore_source_depends_on_when_also_model(dbt_dummy_postgres_config: PostgresConfig):
    context = DbtContext()
    context._target = dbt_dummy_postgres_config

    source_a = SourceConfig(
        name="source_a",
        fqn=["package", "schema", "model_a"],
    )
    source_a._canonical_name = "schema.source_a"
    source_b = SourceConfig(
        name="source_b",
        fqn=["package", "schema", "source_b"],
    )
    source_b._canonical_name = "schema.source_b"
    context.sources = {"source_a": source_a, "source_b": source_b}

    model = ModelConfig(
        dependencies=Dependencies(sources={"source_a", "source_b"}),
        fqn=["package", "schema", "test_model"],
    )
    context.models = {
        "test_model": model,
        "model_a": ModelConfig(name="model_a", fqn=["package", "schema", "model_a"]),
    }

    assert model.sqlmesh_model_kwargs(context)["depends_on"] == {"schema.source_b"}


@pytest.mark.xdist_group("dbt_manifest")
def test_dbt_hooks_with_transaction_flag(sushi_test_dbt_context: Context):
    model_fqn = '"memory"."sushi"."model_with_transaction_hooks"'
    assert model_fqn in sushi_test_dbt_context.models

    model = sushi_test_dbt_context.models[model_fqn]

    pre_statements = model.pre_statements_
    assert pre_statements is not None
    assert len(pre_statements) >= 3

    # we need to check the expected SQL but more importantly that the transaction flags are there
    assert any(
        s.sql == 'JINJA_STATEMENT_BEGIN;\n{{ log("pre-hook") }}\nJINJA_END;'
        and s.transaction is True
        for s in pre_statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS hook_outside_pre_table" in s.sql and s.transaction is False
        for s in pre_statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS shared_hook_table" in s.sql and s.transaction is False
        for s in pre_statements
    )
    assert any(
        "{{ insert_into_shared_hook_table('inside_pre') }}" in s.sql and s.transaction is True
        for s in pre_statements
    )

    post_statements = model.post_statements_
    assert post_statements is not None
    assert len(post_statements) >= 4
    assert any(
        s.sql == 'JINJA_STATEMENT_BEGIN;\n{{ log("post-hook") }}\nJINJA_END;'
        and s.transaction is True
        for s in post_statements
    )
    assert any(
        "{{ insert_into_shared_hook_table('inside_post') }}" in s.sql and s.transaction is True
        for s in post_statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS hook_outside_post_table" in s.sql and s.transaction is False
        for s in post_statements
    )
    assert any(
        "{{ insert_into_shared_hook_table('after_commit') }}" in s.sql and s.transaction is False
        for s in post_statements
    )

    # render_pre_statements with inside_transaction=True should only return inserrt
    inside_pre_statements = model.render_pre_statements(inside_transaction=True)
    assert len(inside_pre_statements) == 1
    assert (
        inside_pre_statements[0].sql()
        == """INSERT INTO "shared_hook_table" ("id", "hook_name", "execution_order", "created_at") VALUES ((SELECT COALESCE(MAX("id"), 0) + 1 FROM "shared_hook_table"), 'inside_pre', (SELECT COALESCE(MAX("id"), 0) + 1 FROM "shared_hook_table"), NOW())"""
    )

    # while for render_pre_statements with inside_transaction=False the create statements
    outside_pre_statements = model.render_pre_statements(inside_transaction=False)
    assert len(outside_pre_statements) == 2
    assert "CREATE" in outside_pre_statements[0].sql()
    assert "hook_outside_pre_table" in outside_pre_statements[0].sql()
    assert "CREATE" in outside_pre_statements[1].sql()
    assert "shared_hook_table" in outside_pre_statements[1].sql()

    # similarly for post statements
    inside_post_statements = model.render_post_statements(inside_transaction=True)
    assert len(inside_post_statements) == 1
    assert (
        inside_post_statements[0].sql()
        == """INSERT INTO "shared_hook_table" ("id", "hook_name", "execution_order", "created_at") VALUES ((SELECT COALESCE(MAX("id"), 0) + 1 FROM "shared_hook_table"), 'inside_post', (SELECT COALESCE(MAX("id"), 0) + 1 FROM "shared_hook_table"), NOW())"""
    )

    outside_post_statements = model.render_post_statements(inside_transaction=False)
    assert len(outside_post_statements) == 2
    assert "CREATE" in outside_post_statements[0].sql()
    assert "hook_outside_post_table" in outside_post_statements[0].sql()
    assert "INSERT" in outside_post_statements[1].sql()
    assert "shared_hook_table" in outside_post_statements[1].sql()


@pytest.mark.xdist_group("dbt_manifest")
def test_dbt_hooks_with_transaction_flag_execution(sushi_test_dbt_context: Context):
    model_fqn = '"memory"."sushi"."model_with_transaction_hooks"'
    assert model_fqn in sushi_test_dbt_context.models

    plan = sushi_test_dbt_context.plan(select_models=["sushi.model_with_transaction_hooks"])
    sushi_test_dbt_context.apply(plan)

    result = sushi_test_dbt_context.engine_adapter.fetchdf(
        "SELECT * FROM sushi.model_with_transaction_hooks"
    )
    assert len(result) == 1
    assert result["id"][0] == 1
    assert result["name"][0] == "test"

    # ensure the outside pre-hook and post-hook table were created
    pre_outside = sushi_test_dbt_context.engine_adapter.fetchdf(
        "SELECT * FROM hook_outside_pre_table"
    )
    assert len(pre_outside) == 1
    assert pre_outside["id"][0] == 1
    assert pre_outside["location"][0] == "outside"
    assert pre_outside["execution_order"][0] == 1

    post_outside = sushi_test_dbt_context.engine_adapter.fetchdf(
        "SELECT * FROM hook_outside_post_table"
    )
    assert len(post_outside) == 1
    assert post_outside["id"][0] == 5
    assert post_outside["location"][0] == "outside"
    assert post_outside["execution_order"][0] == 5

    # verify the shared table that was created by before_begin and populated by all hooks
    shared_table = sushi_test_dbt_context.engine_adapter.fetchdf(
        "SELECT * FROM shared_hook_table ORDER BY execution_order"
    )
    assert len(shared_table) == 3
    assert shared_table["execution_order"].is_monotonic_increasing

    # The order of creation and insertion will verify the following order of execution
    # 1. before_begin (transaction=false) ran BEFORE the transaction started and created the table
    # 2. inside_pre (transaction=true) ran INSIDE the transaction and could insert into the table
    # 3. inside_post (transaction=true) ran INSIDE the transaction and could insert into the table (but after pre statement)
    # 4. after_commit (transaction=false) ran AFTER the transaction committed

    assert shared_table["id"][0] == 1
    assert shared_table["hook_name"][0] == "inside_pre"
    assert shared_table["execution_order"][0] == 1

    assert shared_table["id"][1] == 2
    assert shared_table["hook_name"][1] == "inside_post"
    assert shared_table["execution_order"][1] == 2

    assert shared_table["id"][2] == 3
    assert shared_table["hook_name"][2] == "after_commit"
    assert shared_table["execution_order"][2] == 3

    # the timestamps also should be monotonically increasing for the same reason
    for i in range(len(shared_table) - 1):
        assert shared_table["created_at"][i] <= shared_table["created_at"][i + 1]

    # the tables using the alternate syntax should have correct order as well
    assert pre_outside["created_at"][0] < shared_table["created_at"][0]
    assert post_outside["created_at"][0] > shared_table["created_at"][1]

    # running with execution time one day in the future to simulate a run
    tomorrow = datetime.now() + timedelta(days=1)
    sushi_test_dbt_context.run(
        select_models=["sushi.model_with_transaction_hooks"], execution_time=tomorrow
    )

    # to verify that the transaction information persists in state and is respected
    shared_table = sushi_test_dbt_context.engine_adapter.fetchdf(
        "SELECT * FROM shared_hook_table ORDER BY execution_order"
    )

    # and the execution order for run is similar
    assert shared_table["execution_order"].is_monotonic_increasing
    assert shared_table["id"][3] == 4
    assert shared_table["hook_name"][3] == "inside_pre"
    assert shared_table["execution_order"][3] == 4

    assert shared_table["id"][4] == 5
    assert shared_table["hook_name"][4] == "inside_post"
    assert shared_table["execution_order"][4] == 5

    assert shared_table["id"][5] == 6
    assert shared_table["hook_name"][5] == "after_commit"
    assert shared_table["execution_order"][5] == 6

    for i in range(len(shared_table) - 1):
        assert shared_table["created_at"][i] <= shared_table["created_at"][i + 1]
