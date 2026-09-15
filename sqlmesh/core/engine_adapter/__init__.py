from __future__ import annotations

import typing as t

from sqlmesh.core.engine_adapter.base import (
    EngineAdapter,
    EngineAdapterWithIndexSupport,
)
from sqlmesh.core.engine_adapter.bigquery import BigQueryEngineAdapter
from sqlmesh.core.engine_adapter.clickhouse import ClickhouseEngineAdapter
from sqlmesh.core.engine_adapter.databricks import DatabricksEngineAdapter
from sqlmesh.core.engine_adapter.duckdb import DuckDBEngineAdapter
from sqlmesh.core.engine_adapter.mssql import MSSQLEngineAdapter
from sqlmesh.core.engine_adapter.mysql import MySQLEngineAdapter
from sqlmesh.core.engine_adapter.postgres import PostgresEngineAdapter
from sqlmesh.core.engine_adapter.redshift import RedshiftEngineAdapter
from sqlmesh.core.engine_adapter.snowflake import SnowflakeEngineAdapter
from sqlmesh.core.engine_adapter.spark import SparkEngineAdapter
from sqlmesh.core.engine_adapter.starrocks import StarRocksEngineAdapter
from sqlmesh.core.engine_adapter.trino import TrinoEngineAdapter
from sqlmesh.core.engine_adapter.athena import AthenaEngineAdapter
from sqlmesh.core.engine_adapter.risingwave import RisingwaveEngineAdapter
from sqlmesh.core.engine_adapter.fabric import FabricEngineAdapter

DIALECT_TO_ENGINE_ADAPTER = {
    "hive": SparkEngineAdapter,
    "spark": SparkEngineAdapter,
    "bigquery": BigQueryEngineAdapter,
    "clickhouse": ClickhouseEngineAdapter,
    "duckdb": DuckDBEngineAdapter,
    "snowflake": SnowflakeEngineAdapter,
    "databricks": DatabricksEngineAdapter,
    "redshift": RedshiftEngineAdapter,
    "postgres": PostgresEngineAdapter,
    "mysql": MySQLEngineAdapter,
    "mssql": MSSQLEngineAdapter,
    "trino": TrinoEngineAdapter,
    "athena": AthenaEngineAdapter,
    "risingwave": RisingwaveEngineAdapter,
    "fabric": FabricEngineAdapter,
    "starrocks": StarRocksEngineAdapter,
}

DIALECT_ALIASES = {
    "postgresql": "postgres",
}


def create_engine_adapter(
    connection_factory: t.Callable[[], t.Any], dialect: str, **kwargs: t.Any
) -> EngineAdapter:
    dialect = dialect.lower()
    dialect = DIALECT_ALIASES.get(dialect, dialect)
    engine_adapter = DIALECT_TO_ENGINE_ADAPTER.get(dialect)
    if engine_adapter is None:
        return EngineAdapter(connection_factory, dialect, **kwargs)
    if engine_adapter is EngineAdapterWithIndexSupport:
        return EngineAdapterWithIndexSupport(
            connection_factory,
            dialect,
            **kwargs,
        )
    return engine_adapter(connection_factory, **kwargs)
