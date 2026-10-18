# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import typing as t

import pytest
from sqlglot import exp, parse_one

from sqlmesh.core.config.connection import ClickhouseConnectionConfig
from sqlmesh.core.engine_adapter.clickhouse import ClickhouseEngineAdapter
from sqlmesh.utils.errors import SQLMeshError


pytestmark = [pytest.mark.docker, pytest.mark.clickhouse_datalake]

_RUN_FLAG = "SQLMESH_RUN_CLICKHOUSE_DATALAKE"
_CLICKHOUSE_VERSION = "25.8.16.34"
_REST_PASSWORD = "localpwd"
_EXPECTED_ROWS = [(1, "alpha"), (2, "beta"), (3, "gamma")]
_COLUMN_TYPES = {
    "id": exp.DataType.build("Int64", dialect="clickhouse"),
    "value": exp.DataType.build("String", dialect="clickhouse"),
}
_SOURCE_QUERY = "SELECT id, value FROM lake.analytics.events ORDER BY id"


def _new_adapter() -> ClickhouseEngineAdapter:
    connection_config = ClickhouseConnectionConfig(
        host="127.0.0.1",
        port=18123,
        username="clickhouse",
        password="clickhouse",
        connection_settings={"allow_database_iceberg": True},
        datalake_catalog_mapping={"lake": "iceberg"},
        concurrent_tasks=1,
        register_comments=False,
    )
    adapter = connection_config.create_engine_adapter()
    assert isinstance(adapter, ClickhouseEngineAdapter)
    return adapter


@pytest.fixture(scope="module")
def engine_adapter() -> t.Iterator[ClickhouseEngineAdapter]:
    if os.environ.get(_RUN_FLAG) != "1":
        pytest.skip(f"Set {_RUN_FLAG}=1 after starting the dedicated DataLake Compose stack")

    adapter = _new_adapter()
    try:
        version = adapter.fetchone("SELECT version()")
        assert version is not None and version[0] == _CLICKHOUSE_VERSION

        # Keep the official ClickHouse test's setting and DDL sequence. The mapped database is
        # local to this Compose project and points at its private MinIO bucket.
        adapter.execute("SET allow_database_iceberg=true")
        adapter.execute(
            "CREATE DATABASE IF NOT EXISTS iceberg "
            "ENGINE = DataLakeCatalog('http://rest:8181/v1', 'minio', "
            f"'{_REST_PASSWORD}') "
            "SETTINGS catalog_type = 'rest', warehouse = 'demo', "
            "storage_endpoint = 'http://minio:9000/warehouse-rest'"
        )
        physical_query = parse_one(
            'SELECT id, value FROM iceberg."analytics.events" ORDER BY id',
            dialect="clickhouse",
        )
        assert adapter.fetchall(physical_query) == _EXPECTED_ROWS

        yield adapter
    finally:
        adapter.close()


@pytest.fixture(scope="module")
def injected_engine_adapter(
    engine_adapter: ClickhouseEngineAdapter,
) -> t.Iterator[ClickhouseEngineAdapter]:
    adapter = _new_adapter()
    adapter.inject_virtual_catalog("clickhouse_datalake")
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.mark.parametrize(
    ("adapter_fixture", "query_sql"),
    [
        pytest.param("engine_adapter", _SOURCE_QUERY, id="uninjected-qualified-table"),
        pytest.param(
            "injected_engine_adapter",
            _SOURCE_QUERY,
            id="injected-qualified-table",
        ),
        pytest.param(
            "engine_adapter",
            "SELECT source.id, source.value "
            "FROM lake.analytics.events AS source ORDER BY source.id",
            id="uninjected-table-alias",
        ),
        pytest.param(
            "injected_engine_adapter",
            "SELECT source.id, source.value "
            "FROM lake.analytics.events AS source ORDER BY source.id",
            id="injected-table-alias",
        ),
        pytest.param(
            "engine_adapter",
            "SELECT lake.analytics.events.id, lake.analytics.events.value "
            "FROM lake.analytics.events ORDER BY lake.analytics.events.id",
            id="uninjected-qualified-columns",
        ),
        pytest.param(
            "injected_engine_adapter",
            "SELECT lake.analytics.events.id, lake.analytics.events.value "
            "FROM lake.analytics.events ORDER BY lake.analytics.events.id",
            id="injected-qualified-columns",
        ),
        pytest.param(
            "engine_adapter",
            "WITH source AS (SELECT id, value FROM lake.analytics.events) "
            "SELECT id, value FROM source ORDER BY id",
            id="uninjected-cte",
        ),
        pytest.param(
            "injected_engine_adapter",
            "WITH source AS (SELECT id, value FROM lake.analytics.events) "
            "SELECT id, value FROM source ORDER BY id",
            id="injected-cte",
        ),
    ],
)
def test_reads_mapped_source_queries_without_mutating_ast(
    request: pytest.FixtureRequest, adapter_fixture: str, query_sql: str
) -> None:
    adapter = request.getfixturevalue(adapter_fixture)
    query = parse_one(query_sql, dialect="clickhouse")
    original_query = query.copy()

    assert adapter.fetchall(query) == _EXPECTED_ROWS
    assert query == original_query


def test_raw_string_sql_is_not_rewritten(engine_adapter: ClickhouseEngineAdapter) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    with pytest.raises(DatabaseError) as error:
        engine_adapter.fetchall(_SOURCE_QUERY)
    assert error.value.code in {62, 81}


@pytest.mark.parametrize("adapter_fixture", ["engine_adapter", "injected_engine_adapter"])
def test_ctas_reads_mapped_source(request: pytest.FixtureRequest, adapter_fixture: str) -> None:
    adapter = request.getfixturevalue(adapter_fixture)
    target = "default.sqlmesh_datalake_ctas"
    adapter.drop_table(target, exists=True)
    try:
        adapter.ctas(
            target,
            parse_one(_SOURCE_QUERY, dialect="clickhouse"),
            target_columns_to_types=_COLUMN_TYPES,
        )

        assert (
            adapter.fetchall(
                parse_one(f"SELECT id, value FROM {target} ORDER BY id", dialect="clickhouse")
            )
            == _EXPECTED_ROWS
        )
    finally:
        adapter.drop_table(target, exists=True)


@pytest.mark.parametrize("adapter_fixture", ["engine_adapter", "injected_engine_adapter"])
def test_insert_append_reads_mapped_source(
    request: pytest.FixtureRequest, adapter_fixture: str
) -> None:
    adapter = request.getfixturevalue(adapter_fixture)
    target = "default.sqlmesh_datalake_insert"
    adapter.drop_table(target, exists=True)
    try:
        adapter.create_table(target, _COLUMN_TYPES, exists=False)
        adapter.insert_append(
            target,
            parse_one(_SOURCE_QUERY, dialect="clickhouse"),
            target_columns_to_types=_COLUMN_TYPES,
        )

        assert (
            adapter.fetchall(
                parse_one(f"SELECT id, value FROM {target} ORDER BY id", dialect="clickhouse")
            )
            == _EXPECTED_ROWS
        )
    finally:
        adapter.drop_table(target, exists=True)


@pytest.mark.parametrize("adapter_fixture", ["engine_adapter", "injected_engine_adapter"])
def test_replace_query_reads_mapped_source(
    request: pytest.FixtureRequest, adapter_fixture: str
) -> None:
    adapter = request.getfixturevalue(adapter_fixture)
    target = "default.sqlmesh_datalake_replace"
    adapter.drop_table(target, exists=True)
    try:
        adapter.ctas(
            target,
            parse_one("SELECT 99 AS id, 'stale' AS value", dialect="clickhouse"),
            target_columns_to_types=_COLUMN_TYPES,
        )
        adapter.replace_query(
            target,
            parse_one(_SOURCE_QUERY, dialect="clickhouse"),
            target_columns_to_types=_COLUMN_TYPES,
        )

        assert (
            adapter.fetchall(
                parse_one(f"SELECT id, value FROM {target} ORDER BY id", dialect="clickhouse")
            )
            == _EXPECTED_ROWS
        )
    finally:
        adapter.drop_table(target, exists=True)


def test_unknown_materialization_source_fails_before_target_creation(
    engine_adapter: ClickhouseEngineAdapter,
) -> None:
    target = "default.sqlmesh_datalake_unknown_source"
    engine_adapter.drop_table(target, exists=True)

    try:
        with pytest.raises(SQLMeshError, match="Provided catalog: foreign"):
            engine_adapter.ctas(
                target,
                parse_one("SELECT id, value FROM foreign.analytics.events", dialect="clickhouse"),
                target_columns_to_types=_COLUMN_TYPES,
            )

        assert not engine_adapter.table_exists(target)
    finally:
        engine_adapter.drop_table(target, exists=True)


def test_uninjected_view_maps_source(engine_adapter: ClickhouseEngineAdapter) -> None:
    view = "default.sqlmesh_datalake_view"
    engine_adapter.drop_view(view, ignore_if_not_exists=True)
    try:
        engine_adapter.create_view(view, parse_one(_SOURCE_QUERY, dialect="clickhouse"))

        assert (
            engine_adapter.fetchall(
                parse_one(f"SELECT id, value FROM {view} ORDER BY id", dialect="clickhouse")
            )
            == _EXPECTED_ROWS
        )
    finally:
        engine_adapter.drop_view(view, ignore_if_not_exists=True)


def test_injected_view_rejects_mapped_source(
    injected_engine_adapter: ClickhouseEngineAdapter,
) -> None:
    adapter = injected_engine_adapter
    view = "default.sqlmesh_datalake_injected_view"
    adapter.drop_view(view, ignore_if_not_exists=True)
    try:
        with pytest.raises(SQLMeshError, match="all catalog operations"):
            adapter.create_view(view, parse_one(_SOURCE_QUERY, dialect="clickhouse"))
        assert not adapter.table_exists(view)
    finally:
        adapter.drop_view(view, ignore_if_not_exists=True)


def test_injected_virtual_catalog_is_stripped_from_reads(
    injected_engine_adapter: ClickhouseEngineAdapter,
) -> None:
    target = "default.sqlmesh_datalake_virtual_catalog"
    injected_engine_adapter.drop_table(target, exists=True)
    try:
        injected_engine_adapter.ctas(
            target,
            parse_one("SELECT 1 AS id, 'local' AS value", dialect="clickhouse"),
            target_columns_to_types=_COLUMN_TYPES,
        )
        virtual_query = parse_one(
            "SELECT id, value FROM __clickhouse_datalake__.default.sqlmesh_datalake_virtual_catalog",
            dialect="clickhouse",
        )

        assert injected_engine_adapter.fetchall(virtual_query) == [(1, "local")]
    finally:
        injected_engine_adapter.drop_table(target, exists=True)
