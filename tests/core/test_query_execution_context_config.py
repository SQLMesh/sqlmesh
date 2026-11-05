# SPDX-License-Identifier: Apache-2.0
from unittest.mock import Mock

from sqlmesh.core.config.connection import DuckDBConnectionConfig
from sqlmesh.core.config.scheduler import BuiltInSchedulerConfig
from sqlmesh.core.query_execution import ModelExecutionContext, QueryExecutionContext


def test_cached_duckdb_adapter_honors_query_context_flag_in_config_order(tmp_path):
    database = str(tmp_path / "shared.duckdb")
    disabled = DuckDBConnectionConfig(
        database=database, query_execution_context=False
    ).create_engine_adapter()
    enabled = DuckDBConnectionConfig(
        database=database, query_execution_context=True
    ).create_engine_adapter()

    assert disabled.query_execution_context is False
    assert enabled.query_execution_context is True
    assert enabled is not disabled
    assert enabled._connection_pool is disabled._connection_pool


def test_cached_duckdb_adapter_does_not_leak_provider_to_default_config(tmp_path):
    database = str(tmp_path / "shared.duckdb")
    enabled = DuckDBConnectionConfig(
        database=database, query_execution_context=True
    ).create_engine_adapter()
    provider = lambda: QueryExecutionContext(
        evaluation=ModelExecutionContext("eval-1", "analytics.orders", "snapshot-3")
    )
    enabled.query_execution_context_provider = provider

    disabled = DuckDBConnectionConfig(database=database).create_engine_adapter()

    assert enabled.query_execution_context is True
    assert enabled.query_execution_context_provider is provider
    assert disabled.query_execution_context is False
    assert disabled.query_execution_context_provider is None
    assert disabled is not enabled
    assert disabled._connection_pool is enabled._connection_pool


def test_state_sync_clones_cached_warehouse_adapter_without_query_context(mocker, tmp_path):
    warehouse = DuckDBConnectionConfig(
        database=str(tmp_path / "warehouse.db"),
        query_execution_context=True,
    )
    warehouse_adapter = warehouse.create_engine_adapter()
    warehouse_adapter.query_execution_context_provider = lambda: QueryExecutionContext(
        evaluation=ModelExecutionContext("eval-1", "analytics.orders", "snapshot-3")
    )

    context = Mock()
    context.config.get_state_connection.return_value = None
    context.config.get_connection.return_value = warehouse
    context.config.get_state_schema.return_value = None
    context.connection_config = warehouse
    context.cache_dir = tmp_path / "cache"
    context.console = Mock()
    state_sync = Mock()
    state_sync_factory = mocker.patch(
        "sqlmesh.core.config.scheduler.EngineAdapterStateSync", return_value=state_sync
    )

    actual = BuiltInSchedulerConfig().create_state_sync(context)

    state_adapter = state_sync_factory.call_args.args[0]
    assert actual is state_sync
    assert state_adapter is not warehouse_adapter
    assert state_adapter.query_execution_context is False
    assert state_adapter.query_execution_context_provider is None
    assert warehouse_adapter.query_execution_context is True
    assert warehouse_adapter.query_execution_context_provider is not None


def test_explicit_state_connection_can_enable_parent_query_context(mocker, tmp_path):
    warehouse = DuckDBConnectionConfig(
        database=str(tmp_path / "warehouse.db"), query_execution_context=True
    )
    warehouse_adapter = warehouse.create_engine_adapter()
    provider = lambda: QueryExecutionContext(
        evaluation=ModelExecutionContext("eval-1", "analytics.orders", "snapshot-3")
    )
    warehouse_adapter.query_execution_context_provider = provider
    state_connection = DuckDBConnectionConfig(
        database=str(tmp_path / "warehouse.db"), query_execution_context=True
    )

    context = Mock()
    context.config.get_state_connection.return_value = state_connection
    context.config.get_connection.return_value = warehouse
    context.config.get_state_schema.return_value = None
    context.connection_config = warehouse
    context.cache_dir = tmp_path / "cache"
    context.console = Mock()
    state_sync = Mock()
    state_sync_factory = mocker.patch(
        "sqlmesh.core.config.scheduler.EngineAdapterStateSync", return_value=state_sync
    )

    BuiltInSchedulerConfig().create_state_sync(context)

    state_adapter = state_sync_factory.call_args.args[0]
    assert state_adapter.query_execution_context is True
    assert state_adapter.query_execution_context_provider is None
    assert warehouse_adapter.query_execution_context_provider is provider
