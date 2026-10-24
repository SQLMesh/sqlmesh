# Apache Doris

!!! info
    The Doris engine adapter is a community contribution. Due to this, only limited community support is available.

This page describes SQLMesh support for the [Apache Doris](https://doris.apache.org/) engine (versions 2.1, 3.x, 4.x, and 5.0), including native Doris OLAP tables and Apache Iceberg multi-catalog lakehouse tables.

!!! note
    Doris may not be used for the SQLMesh [state connection](../../reference/configuration.md#connections). Configure a transactional database such as Postgres, MySQL, or DuckDB (for local development) as your `state_connection`.

## Local/Built-in Scheduler

**Engine Adapter Type**: `doris`

### Installation

```bash
pip install "sqlmesh[doris]"
```

### Connection options

| Option | Description | Type | Required |
| --- | --- | :---: | :---: |
| `type` | Engine type name - must be `doris` | string | Y |
| `host` | The hostname of the Doris FE (Frontend) server | string | Y |
| `user` | The username to use for authentication with Doris FE | string | Y |
| `password` | The password to use for authentication with Doris FE (Default: `None`) | string | N |
| `port` | The query port number of the Doris FE server (Default: `9030`) | int | N |
| `database` | The optional default database (schema) name | string | N |
| `catalog` | The optional default catalog name (e.g., `internal` or an external `iceberg` catalog) | string | N |
| `charset` | The character set used for the connection (Default: `utf8mb4`) | string | N |
| `collation` | The collation used for the connection | string | N |
| `connect_timeout` | Connection timeout in seconds (Default: `10`) | int | N |
| `read_timeout` | Read timeout in seconds | int | N |
| `write_timeout` | Write timeout in seconds | int | N |
| `pre_ping` | Whether to ping the connection before executing a statement and reconnect if lost (Default: `False`) | bool | N |
| `session_variables` | Session variables to set on each new connection via `SET <key> = <value>` (e.g., `workload_group`, `query_timeout`) | dict | N |
| `ssl` | Explicit SSL configuration (`True`/`False` or dictionary) passed to `PyMySQL` | bool \| dict | N |
| `ssl_disabled` | Whether SSL is disabled | bool | N |
| `ssl_ca` | Path to the file that contains a PEM-formatted CA certificate | string | N |
| `ssl_cert` | Path to the file that contains a PEM-formatted client certificate | string | N |
| `ssl_key` | Path to the file that contains a PEM-formatted private key for the client certificate | string | N |
| `ssl_verify_cert` | Set to `true` to verify the server's certificate against the CA certificate | bool | N |
| `ssl_verify_identity` | Set to `true` to verify the server's identity in addition to the certificate | bool | N |
| `concurrent_tasks` | The maximum number of tasks that can use this connection concurrently (Default: `4`) | int | N |
| `register_comments` | Whether or not to register model comments with the SQL engine (Default: `True`) | bool | N |

### Connection configuration example

```yaml
gateways:
  doris:
    connection:
      type: doris
      host: fe.doris.cluster
      port: 9030
      user: doris_user
      password: {{ env_var('DORIS_PASSWORD') }}
      database: analytics
      catalog: internal
      connect_timeout: 30
      session_variables:
        workload_group: etl_group
        query_timeout: 3600
    state_connection:
      type: postgres
      host: state-db.internal
      port: 5432
      user: sqlmesh
      password: {{ env_var('POSTGRES_PASSWORD') }}
      database: sqlmesh_state

default_gateway: doris

model_defaults:
  dialect: doris
```

---

## OLAP Table Models

Apache Doris supports three native OLAP table data models: **DUPLICATE KEY**, **UNIQUE KEY**, and **AGGREGATE KEY**.

!!! tip "Automatic Key Column Prefix Reordering"
    Doris requires key columns (`UNIQUE KEY`, `DUPLICATE KEY`, or `AGGREGATE KEY`) to appear as the **first columns** in a `CREATE TABLE` definition in the exact order of the key. SQLMesh's `DorisEngineAdapter` automatically reorders your schema definition during `CREATE TABLE` so key columns appear first, regardless of the column order in your `SELECT` query.

### DUPLICATE KEY model (default)

By default, OLAP tables use the `DUPLICATE KEY` model. If no key is specified, Doris uses the first column as the duplicate key and hash distribution column.

```sql
MODEL (
  name analytics.user_events,
  kind FULL,
  clustered_by (user_id),
  physical_properties (
    duplicate_key = ('user_id', 'event_time'),
    buckets = 10
  )
);

SELECT
  user_id::BIGINT AS user_id,
  event_time::DATETIME AS event_time,
  event_type::VARCHAR(64) AS event_type
FROM raw.events;
```

### UNIQUE KEY model (Merge-on-Write & custom sort keys)

For `INCREMENTAL_BY_UNIQUE_KEY` models, SQLMesh automatically configures the model's `unique_key` as the Doris table's `UNIQUE KEY` and `DISTRIBUTED BY HASH(...)` key—no extra `physical_properties` required:

```sql
MODEL (
  name analytics.orders_upsert,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key order_id
  ),
  physical_properties (
    order_by = ('updated_at', 'order_id'),
    enable_unique_key_merge_on_write = 'true'
  )
);

SELECT
  order_id::BIGINT AS order_id,
  customer_id::BIGINT AS customer_id,
  status::VARCHAR(32) AS status,
  updated_at::DATETIME AS updated_at
FROM raw.orders
WHERE updated_at BETWEEN @start_ds AND @end_ds;
```

You can also specify `unique_key` explicitly in `physical_properties` for `FULL` or `INCREMENTAL_BY_TIME_RANGE` models, and optionally decouple the sort key from the unique key using `order_by` (emitting `ORDER BY (col1, col2)` on Doris 2.1+ Merge-on-Write tables).

### AGGREGATE KEY model

For pre-aggregated rollup tables, specify the key columns using `aggregate_key` in `physical_properties`. Note that Doris requires non-key value columns in an `AGGREGATE KEY` table to specify an aggregation function (`SUM`, `MAX`, `MIN`, `REPLACE`, etc.) in the column definition unless all columns are part of `aggregate_key` (or the table is created via a custom DDL / pre-statement):

```sql
MODEL (
  name analytics.daily_site_keys,
  kind FULL,
  clustered_by (site_id),
  physical_properties (
    aggregate_key = ('site_id', 'event_date'),
    buckets = 16
  )
);

SELECT
  site_id::INT AS site_id,
  event_date::DATE AS event_date
FROM analytics.user_events
GROUP BY site_id, event_date;
```

---

## Distribution & Bucketing

Doris requires a `DISTRIBUTED BY` clause on every OLAP table. SQLMesh supports three ways to configure distribution:

1. **SQLMesh `clustered_by` + optional `buckets`** (recommended for cross-engine portability):
   ```sql
   MODEL (
     name analytics.events,
     kind FULL,
     clustered_by (user_id),
     physical_properties (
       buckets = 16
     )
   );
   ```
2. **Shorthand string `distributed_by`**:
   ```sql
   physical_properties (
     distributed_by = 'HASH(user_id) BUCKETS 16'
     -- or 'RANDOM BUCKETS AUTO'
   )
   ```
3. **Structured dictionary or column tuple `distributed_by`**:
   ```sql
   physical_properties (
     distributed_by = (
       kind = 'HASH',
       expressions = 'user_id',
       buckets = 'AUTO'
     )
     -- Also supports `columns = ('user_id', 'event_time')` or `distributed_by = ('user_id', 'event_time')`,
     -- or `random = 'true'` for `DISTRIBUTED BY RANDOM` on DUPLICATE KEY tables.
   )
   ```

If none of `clustered_by` or `distributed_by` is provided:
- When `unique_key` is present, SQLMesh defaults to `DISTRIBUTED BY HASH(<first_unique_key>) BUCKETS <buckets|10>`.
- When `buckets` is specified with `duplicate_key` or `aggregate_key`, SQLMesh emits `DISTRIBUTED BY HASH(<first_key>) BUCKETS <buckets>` (or `DISTRIBUTED BY RANDOM BUCKETS <buckets>` when no key is specified).

---

## Partitioning

Doris OLAP tables support **Multi-Range Batch Partitioning**, **Explicit Partition Definitions**, **Dynamic Partitioning**, and **Auto Partitioning** (Doris 2.1+). You can specify the partition expression via the top-level `partitioned_by` model property or via `partitioned_by` / `partition_by` inside `physical_properties`.

!!! note "OLAP Partitioning Requirements"
    - On Doris OLAP tables, `PARTITION BY` requires at least one of `partitions`, `auto_partition = 'true'`, or `'dynamic_partition.enable' = 'true'` in `physical_properties`. If none is provided, SQLMesh omits the `PARTITION BY` clause so Doris does not create a zero-partition table that rejects inserts.
    - On `UNIQUE KEY` and `AGGREGATE KEY` tables, Doris requires all `partitioned_by` columns to be a subset of the key columns.

### Multi-range & explicit partitions

```sql
MODEL (
  name analytics.partitioned_events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date
  ),
  partitioned_by RANGE(event_date),
  physical_properties (
    partitions = 'FROM ("2023-01-01") TO ("2027-01-01") INTERVAL 1 MONTH'
  )
);
```

### Auto partitioning (`auto_partition = true`)

In Doris 2.1+, `AUTO PARTITION` automatically creates partitions on demand as new data arrives:

```sql
MODEL (
  name analytics.auto_partitioned_events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_time
  ),
  partitioned_by RANGE(DATE_TRUNC(event_time, 'day')),
  physical_properties (
    auto_partition = 'true'
  )
);
```

---

## Apache Iceberg & Multi-Catalog Lakehouse Integration

Apache Doris 2.1–5.0 acts as a federated lakehouse engine that can both query and write to external **Apache Iceberg** catalogs (REST/Polaris, AWS Glue, Hive Metastore, Hadoop/S3, DLF).

### Pattern 1: Reading Iceberg Bronze tables into Doris OLAP Silver/Gold models

Because `DorisEngineAdapter` supports multi-catalog 3-part naming (`catalog.db.table`), your SQLMesh models can query external Iceberg tables directly while materializing sub-second serving tables in Doris's `internal` OLAP catalog:

```sql
MODEL (
  name silver.user_sessions,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column session_date
  )
);

SELECT
  user_id,
  session_id,
  CAST(event_ts AS DATE) AS session_date
FROM iceberg_catalog.bronze.clickstream
WHERE event_ts BETWEEN @start_ts AND @end_ts;
```

### Pattern 2: Materializing Apache Iceberg tables via Doris

SQLMesh can also create, partition, and overwrite Apache Iceberg tables directly through Doris (`ENGINE=iceberg` and `INSERT OVERWRITE TABLE`).

!!! important "Virtual Environments & Catalog Bootstrap on External Iceberg Catalogs"
    Doris only supports `CREATE VIEW` inside the `internal` catalog; external Iceberg catalogs do not support Doris views, and Doris does not support `environment_suffix_target: catalog`.
    When materializing models directly inside an external Iceberg catalog:

    - Set `virtual_environment_mode: dev_only` (and keep `environment_suffix_target: schema` or `table`) in `config.yaml` so production tables are materialized directly in the Iceberg catalog without virtual views.
    - Configure `environment_catalog_mapping` in `config.yaml` to route non-`prod` virtual environment views to the `internal` catalog (otherwise `DorisEngineAdapter` will raise an error when attempting to create views in an external catalog):
      ```yaml
      virtual_environment_mode: dev_only
      environment_suffix_target: schema
      environment_catalog_mapping:
        "^(?!prod$).*": internal
      before_all:
        - >
          CREATE CATALOG IF NOT EXISTS iceberg_catalog PROPERTIES (
            'type' = 'iceberg',
            'iceberg.catalog.type' = 'rest',
            'uri' = 'http://rest-catalog:8181',
            'warehouse' = 's3://lakehouse/warehouse'
          )
      ```

```sql
MODEL (
  name iceberg_catalog.silver.curated_events,
  kind FULL,
  table_format iceberg,
  partitioned_by (day(event_ts), bucket(16, user_id)),
  physical_properties (
    'write-format' = 'parquet',
    'compression-codec' = 'zstd'
  )
);

SELECT
  user_id::BIGINT AS user_id,
  event_ts::DATETIME AS event_ts,
  payload::STRING AS payload
FROM iceberg_catalog.bronze.raw_events;
```

---

## DDL-Only / Schema Contract Management for Streaming Tables (`doris_ddl_only`)

In real-time architectures where **Apache Flink (`flink-doris-connector`)**, **Kafka Routine Load**, **Stream Load**, or **Spark** streams data continuously into a Doris OLAP or Iceberg table, you may want SQLMesh to manage **only the table DDL (`CREATE TABLE`) and schema evolution (`ALTER TABLE`)** without executing `INSERT` or `REPLACE` statements during `sqlmesh run`.

You can achieve this by adding a lightweight `CustomMaterialization` plugin (`materializations/doris_ddl_only.py`) to your SQLMesh project:

```python
from __future__ import annotations

import typing as t
from sqlmesh.core.model import Model
from sqlmesh.core.snapshot.evaluator import CustomMaterialization
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter._typing import QueryOrDF


class DorisDDLOnlyMaterialization(CustomMaterialization):
    """Manages CREATE TABLE and ALTER TABLE schema migrations without inserting data."""

    NAME = "doris_ddl_only"

    def create(
        self,
        table_name: str,
        model: Model,
        is_table_deployable: bool,
        render_kwargs: t.Dict[str, t.Any],
        skip_grants: bool = False,
        **kwargs: t.Any,
    ) -> None:
        if not model.annotated:
            raise SQLMeshError(
                f"Model '{model.name}' uses 'doris_ddl_only' and must explicitly declare `columns (...)` in MODEL (...)."
            )
        super().create(
            table_name=table_name,
            model=model,
            is_table_deployable=is_table_deployable,
            render_kwargs=render_kwargs,
            skip_grants=skip_grants,
            **kwargs,
        )

    def insert(
        self,
        table_name: str,
        query_or_df: QueryOrDF,
        model: Model,
        is_first_insert: bool,
        render_kwargs: t.Dict[str, t.Any],
        **kwargs: t.Any,
    ) -> None:
        # No-op: external Flink / Routine Load / Spark pipelines write data to this table.
        return

    def append(
        self,
        table_name: str,
        query_or_df: QueryOrDF,
        model: Model,
        render_kwargs: t.Dict[str, t.Any],
        **kwargs: t.Any,
    ) -> None:
        # No-op: external Flink / Routine Load / Spark pipelines write data to this table.
        return
```

Then define your streaming target table in `models/` with `virtual_environment_mode: dev_only`:

```sql
MODEL (
  name realtime.flink_order_events,
  kind CUSTOM (
    materialization 'doris_ddl_only',
    forward_only true,
    disable_restatement true
  ),
  columns (
    order_id BIGINT,
    user_id BIGINT,
    amount DECIMAL(18, 2),
    updated_at DATETIME
  ),
  physical_properties (
    unique_key = ('order_id'),
    order_by = ('updated_at', 'order_id'),
    buckets = 16,
    enable_unique_key_merge_on_write = 'true',
    enable_light_schema_change = 'true'
  )
);

SELECT
  CAST(NULL AS BIGINT) AS order_id,
  CAST(NULL AS BIGINT) AS user_id,
  CAST(NULL AS DECIMAL(18, 2)) AS amount,
  CAST(NULL AS DATETIME) AS updated_at;
```

!!! tip "Zero-Downtime Schema Evolution with Flink / Routine Load"
    Setting `forward_only true` and `disable_restatement true` on a `doris_ddl_only` model ensures that schema additions (`ALTER TABLE ... ADD COLUMN`) are applied in-place on the existing table (`enable_light_schema_change = 'true'`) rather than creating a new empty physical table, and prevents accidental restatement wipes.

---

## Compute-Storage Decoupled Mode (Doris 3.0 / 4.x / 5.0)

For Doris 3.0+ Compute-Storage Decoupled deployments on object storage (S3, GCS, Azure Blob, OSS) or HDFS:

- Pass `storage_vault_name` in `physical_properties` (or `model_defaults.physical_properties` in `config.yaml`) to bind tables to a specific storage vault.
- Use `session_variables` in your `doris` connection config to bind SQLMesh workloads to a specific `workload_group` or cloud cluster.

```sql
MODEL (
  name analytics.cloud_events,
  kind FULL,
  clustered_by (event_id),
  physical_properties (
    storage_vault_name = 'prod_s3_vault',
    file_cache_ttl_seconds = '86400'
  )
);
```

---

## Materialized Views & Indexing

### Asynchronous Materialized Views (MTMV)

```sql
MODEL (
  name analytics.user_summary_mv,
  kind VIEW (
    materialized true
  ),
  partitioned_by ds,
  physical_properties (
    build = 'IMMEDIATE',
    refresh = 'AUTO',
    refresh_trigger = 'ON SCHEDULE EVERY 1 HOUR',
    distributed_by = 'HASH(user_id) BUCKETS 10',
    excluded_trigger_tables = 'analytics.user_events'
  )
);

SELECT
  user_id,
  CAST(event_time AS DATE) AS ds,
  COUNT(*) AS event_count
FROM analytics.user_events
GROUP BY user_id, CAST(event_time AS DATE);
```

!!! note "Automatic Physical Table Resolution & Non-Destructive Evaluation"
    - Doris materialized views are only created on initial deployment or when a rebuild is required (`RECREATE_MATERIALIZED_VIEW_ON_EVALUATION = False`), preserving MTMV refresh state across routine `sqlmesh run` invocations.
    - When `excluded_trigger_tables` (supports a single string, comma-separated string, or tuple `('db.t1', 'db.t2')` on async MTMVs) or `source_table` (on synchronous MVs) references a SQLMesh-managed model, SQLMesh automatically resolves the model name to its underlying physical snapshot table name.

### Synchronous Materialized Views (`materialized_type = 'SYNC'`)

Doris also supports single-table synchronous materialized views (rollup indexes maintained atomically on base table writes) by setting `materialized_type = 'SYNC'` and `source_table` in `physical_properties`:

```sql
MODEL (
  name analytics.user_events_sync_mv,
  kind VIEW (
    materialized true
  ),
  physical_properties (
    materialized_type = 'SYNC',
    source_table = 'analytics.user_events'
  )
);

SELECT
  user_id,
  COUNT(event_type) AS event_count
FROM analytics.user_events
GROUP BY user_id;
```

### Inverted Indexes (Doris 2.1–5.0)

You can attach Doris inverted indexes (`USING INVERTED`) for full-text search and fast point filtering using post-statements gated on `@runtime_stage = 'creating'`:

```sql
MODEL (
  name analytics.searchable_logs,
  kind FULL
);

SELECT
  log_id::BIGINT AS log_id,
  message::STRING AS message
FROM raw.logs;

@IF(
  @runtime_stage = 'creating',
  ALTER TABLE @this_model ADD INDEX idx_message (message) USING INVERTED COMMENT 'Full-text inverted index'
);
```

---

## Migrating from `dbt-doris` or StarRocks

If you are migrating an existing `dbt-doris` project or StarRocks SQLMesh models to Doris, map your configuration keys as follows:

| `dbt-doris` / StarRocks Config | SQLMesh Doris Equivalent |
| --- | --- |
| `materialized='table'` | `kind FULL` |
| `materialized='incremental'` (with `unique_key`) | `kind INCREMENTAL_BY_UNIQUE_KEY (unique_key ...)` |
| `materialized='incremental'` (partition overwrite) | `kind INCREMENTAL_BY_TIME_RANGE (time_column ...)` or `kind INCREMENTAL_BY_PARTITION` |
| `duplicate_key=['c1', 'c2']` | `physical_properties (duplicate_key = ('c1', 'c2'))` |
| `unique_key=['c1']` | `kind INCREMENTAL_BY_UNIQUE_KEY (unique_key c1)` or `physical_properties (unique_key = ('c1'))` |
| `table_type='PRIMARY' \| 'UNIQUE' \| 'DUPLICATE' \| 'AGGREGATE'`, `keys=['c1', 'c2']` | Supported directly in `physical_properties` (`PRIMARY` maps to Doris `UNIQUE KEY`) |
| `distributed_by=['c1']`, `buckets=10` | `clustered_by (c1), physical_properties (buckets = 10)` or `physical_properties (distributed_by = ('c1'), buckets = 10)` |
| `partition_by` | Supported in `physical_properties (partition_by = ...)` or top-level `partitioned_by` |
| `properties={...}` | Key-value entries inside `physical_properties (...)` |
