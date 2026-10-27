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

### 5-Minute Quickstart

1. **Initialize a new Doris project** (generates `config.yaml` with `dialect: doris` and a local DuckDB `state_connection`):
   ```bash
   mkdir doris_demo && cd doris_demo
   sqlmesh init doris
   ```
2. **Single-BE Docker / Local Dev Tip**: Doris defaults to `replication_num = 3`. When running against a single-BE Docker container, set `replication_num = 1` in `model_defaults.physical_properties` inside `config.yaml`:
   ```yaml
   model_defaults:
     dialect: doris
     start: 2024-01-01
     physical_properties:
       replication_num: 1
   ```
3. **Plan and apply**:
   ```bash
   sqlmesh plan
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
| `light_schema_change_wait_timeout` | Maximum time in seconds to poll `SHOW ALTER TABLE COLUMN` when an `ALTER TABLE` statement triggers an asynchronous Doris schema change job (Default: `300.0`) | float | N |
| `light_schema_change_poll_interval` | Interval in seconds between `SHOW ALTER TABLE COLUMN` status polls during an asynchronous Doris schema change job (Default: `0.5`) | float | N |
| `concurrent_tasks` | The maximum number of tasks that can use this connection concurrently (Default: `4`) | int | N |
| `register_comments` | Whether or not to register model comments with the SQL engine (Default: `True`) | bool | N |

### Connection configuration example

=== "Local Development (DuckDB State)"

    ```yaml
    gateways:
      doris:
        connection:
          type: doris
          host: 127.0.0.1
          port: 9030
          user: root
          password: ""
          database: analytics
        state_connection:
          type: duckdb
          database: state.db

    default_gateway: doris

    model_defaults:
      dialect: doris
      physical_properties:
        replication_num: 1
    ```

=== "Production (Postgres / MySQL State)"

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

You can also specify `unique_key` explicitly in `physical_properties` for `FULL` or `INCREMENTAL_BY_TIME_RANGE` models, and optionally decouple the sort key from the unique key using `order_by` or `cluster_by` (emitting `ORDER BY (col1, col2)` on Doris Merge-on-Write tables).

On Doris 4.x+, `INCREMENTAL_BY_UNIQUE_KEY` models also support `when_matched` and `merge_filter` via Doris's native `MERGE INTO` syntax (automatically excluding `unique_key` columns from `WHEN MATCHED THEN UPDATE SET` to satisfy Doris Merge-on-Write value-column constraints):

```sql
MODEL (
  name analytics.orders_upsert,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key order_id,
    merge_filter source.updated_at > target.updated_at,
    when_matched WHEN MATCHED THEN UPDATE SET
      target.status = source.status,
      target.updated_at = source.updated_at
  )
);
```

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

In Doris 2.1+, `AUTO PARTITION` automatically creates partitions on demand as new data arrives (and SQLMesh automatically marks the range partition column `NOT NULL` and wraps CTAS projections in `NON_NULLABLE(...)`):

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

### Dynamic partitioning (`dynamic_partition.enable = 'true'`)

For rolling retention windows managed by Doris FE's dynamic partition scheduler:

```sql
MODEL (
  name analytics.rolling_daily_events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date
  ),
  partitioned_by RANGE(event_date),
  physical_properties (
    'dynamic_partition.enable' = 'true',
    'dynamic_partition.time_unit' = 'DAY',
    'dynamic_partition.start' = '-30',
    'dynamic_partition.end' = '3',
    'dynamic_partition.prefix' = 'p',
    'dynamic_partition.buckets' = '16'
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

### Pattern 3: Iceberg Time Travel, Branches/Tags (`@branch` / `@tag`), & Compaction

SQLMesh's Doris dialect natively parses and transpiles Apache Iceberg snapshot time travel, branch/tag reads, branch/tag DDL, and table maintenance/compaction actions while preserving upstream table lineage:

- **Time Travel (`FOR TIME AS OF` / `FOR VERSION AS OF`)**:
  ```sql
  SELECT user_id, event_ts
  FROM iceberg_catalog.bronze.clickstream FOR TIME AS OF '2026-01-15 00:00:00';

  SELECT user_id, event_ts
  FROM iceberg_catalog.bronze.clickstream FOR VERSION AS OF 8493021948102;
  ```
- **Branch & Tag Reads (`@branch(...)` / `@tag(...)`)**:
  ```sql
  SELECT user_id, event_ts
  FROM iceberg_catalog.bronze.clickstream@branch(audit_branch) AS c;

  SELECT user_id, event_ts
  FROM iceberg_catalog.bronze.clickstream@tag(v1_release) AS c;
  ```
- **Iceberg Compaction (`rewrite_data_files`) & Branch Management via Post-Statements**:
  You can run Iceberg file compaction, snapshot expiration, orphan file cleanup, or branch/tag management in model post-statements:
  ```sql
  MODEL (
    name iceberg_catalog.silver.curated_events,
    kind INCREMENTAL_BY_TIME_RANGE (
      time_column event_ts
    ),
    table_format iceberg,
    partitioned_by (day(event_ts))
  );

  SELECT
    user_id::BIGINT AS user_id,
    event_ts::DATETIME AS event_ts,
    payload::STRING AS payload
  FROM iceberg_catalog.bronze.raw_events
  WHERE event_ts BETWEEN @start_ts AND @end_ts;

  @IF(
    @runtime_stage = 'evaluating',
    ALTER TABLE @this_model EXECUTE rewrite_data_files("target-file-size-bytes" = "134217728")
  );
  ```

### Pattern 4: Reading & Writing External Apache Paimon and Apache Hive Catalogs

In addition to Apache Iceberg, `DorisEngineAdapter` supports reading and writing external **Apache Paimon** (`'type' = 'paimon'`, `table_format paimon`) and **Apache Hive** (`'type' = 'hms'`, `table_format hive`) catalogs:

- **Apache Paimon (`table_format paimon`)**: Emits `ENGINE=paimon`, maps `storage_format` to `'file.format'` (`'parquet'` or `'orc'`), and automatically populates Paimon's `'primary-key'` property from `unique_key` (including partition columns when `partitioned_by` is set) so Doris 4.1+ row-level `DELETE` and `MERGE INTO` work out of the box.
- **Apache Hive (`table_format hive`)**: Emits `ENGINE=hive` and maps `storage_format` to `'file_format'` (`'parquet'`, `'orc'`, or `'text'`). Supports `FULL` (`INSERT OVERWRITE TABLE`) and append workloads (`INCREMENTAL_UNMANAGED`), while row-level `DELETE` and `MERGE INTO` are not supported by Hive catalogs in Doris.

```sql
MODEL (
  name paimon_catalog.silver.orders_paimon,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key order_id
  ),
  table_format paimon,
  storage_format parquet,
  partitioned_by (ds)
);

SELECT
  order_id::BIGINT AS order_id,
  amount::DECIMAL(18, 2) AS amount,
  ds::DATE AS ds
FROM hive_catalog.raw.orders;
```

### Pattern 5: Reading External Lance, Hudi & Delta Lake Tables (Doris 4.1+)

Apache Doris 4.1.4+ supports reading multimodal and vector datasets stored in **Lance** format (`'type' = 'lance'` catalog or `s3(...)` / `local(...)` table-valued functions with `'format' = 'lance'`), as well as external **Apache Hudi** (`'type' = 'hudi'`) and **Delta Lake** (`'type' = 'deltalake'`) catalogs.

```yaml
# config.yaml
before_all:
  - >
    CREATE CATALOG IF NOT EXISTS lance_catalog PROPERTIES (
      'type' = 'lance',
      'lance.catalog.type' = 'filesystem',
      'warehouse' = 's3://ai-lakehouse/lance_warehouse'
    )
```

```sql
MODEL (
  name analytics.image_embeddings_olap,
  kind FULL,
  physical_properties (
    storage_format = 'V3'
  )
);

SELECT
  doc_id::BIGINT AS doc_id,
  caption::STRING AS caption,
  embedding::ARRAY<FLOAT> AS embedding,
  L2_DISTANCE(embedding, [0.1, 0.2, 0.3, 0.4]) AS ref_dist
FROM lance_catalog.multimodal.clip_embeddings;
```

!!! warning "Lance, Hudi, and Delta Lake Tables are Read-Only in Doris"
    Doris supports Lance, Hudi, Delta Lake, and LakeSoul catalogs and `format = 'lance'` table-valued functions for reading only (`CREATE TABLE`, `INSERT`, and `CTAS` are not supported by Doris on those formats). If a SQLMesh model sets `table_format` (or `engine`) to `lance`, `hudi`, `deltalake`, or `lakesoul`, `DorisEngineAdapter` raises a descriptive `SQLMeshError` directing you to materialize into the `internal` OLAP catalog or a writable external catalog (`iceberg`, `paimon`, or `hive`).

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
    - When `build = 'DEFERRED'` is used on an asynchronous MTMV that also defines `audits (...)`, SQLMesh automatically executes `REFRESH MATERIALIZED VIEW <mv> AUTO` after creation so audits evaluate against populated data rather than an empty view.
    - When `excluded_trigger_tables` (supports a single string, comma-separated string, or tuple `('db.t1', 'db.t2')` on async MTMVs) or `source_table` (on synchronous MVs) references a SQLMesh-managed model, SQLMesh automatically resolves the model name to its underlying physical snapshot table name.

### Synchronous Materialized Views (`materialized_type = 'SYNC'`)

Doris also supports single-table synchronous materialized views (rollup indexes maintained atomically on a single `internal`-catalog OLAP base table; queries with `JOIN`s or external catalog tables must use asynchronous MTMVs instead) by setting `materialized_type = 'SYNC'` and `source_table` in `physical_properties`:

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

### Inverted & Vector Indexes, Full-Text Search & Compaction (Doris 2.1–5.0)

You can attach Doris inverted indexes (`USING INVERTED` with tokenizer `PROPERTIES`), bloom filter indexes (`USING NGRAM_BF`), or Doris 4.x+ ANN vector indexes (`USING ANN` with `hnsw` or `ivf` index types) and trigger `BUILD INDEX` using post-statements. You can also configure OLAP `compaction_policy` (`'time_series'` or `'size_based'`) and `function_column.sequence_col` in `physical_properties`:

```sql
MODEL (
  name analytics.searchable_logs,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key log_id
  ),
  physical_properties (
    -- Optional Doris 4.1+ Segment V3 format for wide tables
    storage_format = 'V3',
    -- Use 'time_series' compaction policy to reduce write amplification on append/log workloads
    compaction_policy = 'time_series',
    -- Sequence column prevents out-of-order updates with lower sequence values from overwriting newer rows
    'function_column.sequence_col' = 'updated_at'
  )
);

SELECT
  log_id::BIGINT AS log_id,
  message::STRING AS message,
  embedding::ARRAY<FLOAT> AS embedding,
  updated_at::DATETIME AS updated_at,
  created_at::TIMESTAMPTZ(6) AS created_at
FROM raw.logs;

@IF(
  @runtime_stage = 'creating',
  ALTER TABLE @this_model ADD INDEX idx_message (message) USING INVERTED PROPERTIES ("parser" = "english", "support_phrase" = "true") COMMENT 'Full-text inverted index'
);

@IF(
  @runtime_stage = 'creating',
  ALTER TABLE @this_model ADD INDEX idx_embedding (embedding) USING ANN PROPERTIES ("index_type" = "hnsw", "metric_type" = "l2_distance", "dim" = "768") COMMENT 'ANN vector index'
);

@IF(
  @runtime_stage = 'evaluating',
  BUILD INDEX idx_message ON @this_model
);
```

Downstream SQLMesh models can query full-text and vector indexes using Doris's native search operators (`MATCH`, `MATCH_ANY`, `MATCH_ALL`, `MATCH_PHRASE`, `MATCH_PHRASE_PREFIX`, `MATCH_REGEXP`, `MATCH_PHRASE_EDGE`, `SEARCH(...)`) and vector distance functions (`L2_DISTANCE`, `COSINE_DISTANCE`, `INNER_PRODUCT`, `L1_DISTANCE`, and their `_APPROXIMATE` counterparts):

```sql
SELECT
  log_id,
  L2_DISTANCE_APPROXIMATE(embedding, [0.12, -0.04, 0.88]) AS dist
FROM analytics.searchable_logs
WHERE message MATCH_PHRASE 'connection timeout'
  AND SEARCH('message:timeout')
ORDER BY dist ASC
LIMIT 20;
```

---

## Audits & Data Diff (`sqlmesh audit` & `sqlmesh table_diff`)

All built-in and custom SQLMesh [audits](../../concepts/audits.md) (`not_null`, `unique_values`, `number_of_rows`, `accepted_values`, `forall`, etc.) and [`sqlmesh table_diff`](../../guides/tablediff.md) work out of the box on Doris:

```sql
MODEL (
  name analytics.daily_orders,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column order_date,
    partition_by_time_column false
  ),
  audits (
    not_null(columns := (order_id, customer_id)),
    unique_values(columns := (order_id)),
    number_of_rows(threshold := 1)
  )
);
```

- **Constant folding in `LIMIT` clauses**: Doris requires `LIMIT` arguments to be integer literals rather than arithmetic expressions (such as `LIMIT 10 + 1` emitted by `number_of_rows`). `DorisEngineAdapter` automatically folds constant arithmetic in `LIMIT` clauses before execution.
- **Deferred Materialized Views (`build = 'DEFERRED'`)**: When a materialized view is created with `build = 'DEFERRED'` and has `audits (...)` attached, SQLMesh automatically triggers `REFRESH MATERIALIZED VIEW ... AUTO` before running audits so they evaluate against populated data.
- **`sqlmesh table_diff` on floating-point and string columns**: Temporary diff tables created by `sqlmesh table_diff` automatically promote integer indicator columns into the Doris OLAP short-key prefix, so comparing tables whose leading user columns are `DOUBLE`, `FLOAT`, `STRING`, or `JSON` succeeds without manual key configuration.

---

## Supported Model Kinds & Catalog Matrix

| SQLMesh Model Kind | Doris Internal OLAP Catalog (`internal` on Local Disk or S3/GCS Storage Vault) | External Iceberg & Paimon Catalogs (`table_format iceberg \| paimon` on S3/GCS/HDFS) | External Hive Catalog (`table_format hive` on S3/GCS/HDFS) | External Read-Only Lakehouse Catalogs (`lance`, `hudi`, `deltalake`, `lakesoul`) |
| --- | :---: | :---: | :---: | :---: |
| Source / Upstream `SELECT` | Supported | Supported (incl. `FOR TIME/VERSION AS OF`, `@branch`, `@tag`) | Supported | Supported (Read-Only via corresponding External Catalog; Lance also supports `s3()` / `local()` TVFs) |
| `FULL` | Supported (`CREATE TABLE ... AS SELECT` / `DELETE` + `INSERT`) | Supported (`CREATE TABLE ... ENGINE=iceberg\|paimon` / `INSERT OVERWRITE TABLE`) | Supported (`CREATE TABLE ... ENGINE=hive` / `INSERT OVERWRITE TABLE`) | Read-Only in Doris (Write Guarded) |
| `INCREMENTAL_BY_TIME_RANGE` | Supported (`DELETE WHERE time_col >= low AND time_col <= high` + `INSERT`) | Supported (`DELETE` + `INSERT`; requires Doris 4.1.0+ & Iceberg v2 / Paimon PK) | Not supported by Hive in Doris (No row-level `DELETE`) | Read-Only in Doris (Write Guarded) |
| `INCREMENTAL_BY_UNIQUE_KEY` | Supported (Auto-promotes `unique_key` to `UNIQUE KEY`, `DELETE ... USING` + `INSERT` or native `MERGE INTO` when `when_matched` / `merge_filter` is specified) | Supported (Batched key `DELETE` + `INSERT` or native `MERGE INTO`; requires Doris 4.1.0+ & Iceberg v2 / Paimon PK) | Not supported by Hive in Doris (No `DELETE` / `MERGE INTO`) | Read-Only in Doris (Write Guarded) |
| `INCREMENTAL_BY_PARTITION` | Supported (Column & time-truncation range `DELETE` + `INSERT`) | Supported (Partition `DELETE` + `INSERT`; requires Doris 4.1.0+ & Iceberg v2 / Paimon PK) | Not supported by Hive in Doris (No `DELETE`) | Read-Only in Doris (Write Guarded) |
| `SCD_TYPE_2_BY_TIME` / `SCD_TYPE_2_BY_COLUMN` | Supported | Supported (Requires Doris 4.1.0+ & Iceberg v2 / Paimon PK for row-level `DELETE`) | Not supported by Hive in Doris (No `DELETE`) | Read-Only in Doris (Write Guarded) |
| `VIEW` | Supported (`CREATE OR REPLACE VIEW`) | Requires `internal` catalog (`environment_catalog_mapping`) | Requires `internal` catalog (`environment_catalog_mapping`) | Requires `internal` catalog (`environment_catalog_mapping`) |
| `VIEW (materialized true)` | Supported (Async MTMV & Sync MV with `materialized_type = 'SYNC'`) | Not supported by Doris inside external catalogs | Not supported by Doris inside external catalogs | Not supported by Doris inside external catalogs |
| `SEED` / Python DataFrame Models | Supported (Automatic `VARCHAR(255)` OLAP key coercion) | Supported | Supported | Read-Only in Doris (Write Guarded) |
| `CUSTOM ('doris_ddl_only')` | Supported (DDL + `ALTER TABLE` schema evolution only) | Supported | Supported | Not supported (Read-Only) |

---

## Property Aliases & StarRocks Compatibility

If you are migrating StarRocks SQLMesh models or existing Doris DDL conventions, `DorisEngineAdapter` supports these property shorthands in `physical_properties`:

| Property / Shorthand | SQLMesh Doris Behavior |
| --- | --- |
| `duplicate_key = ('c1', 'c2')` | Emits `DUPLICATE KEY (c1, c2)` (and automatically appends active OLAP `partitioned_by` columns if omitted from `duplicate_key`) |
| `unique_key = ('c1')` | Emits `UNIQUE KEY (c1)` (automatic for `INCREMENTAL_BY_UNIQUE_KEY`) |
| `aggregate_key = ('c1', 'c2')` | Emits `AGGREGATE KEY (c1, c2)` |
| `table_type = 'PRIMARY' \| 'UNIQUE' \| 'DUPLICATE' \| 'AGGREGATE'`, `keys = ('c1', 'c2')` | Maps directly to the corresponding Doris key model (`PRIMARY` maps to Doris `UNIQUE KEY`) |
| `clustered_by (c1)` + `physical_properties (buckets = 10)` | Emits `DISTRIBUTED BY HASH(c1) BUCKETS 10` |
| `physical_properties (partition_by = ...)` | Supported alias for top-level `partitioned_by` |

---

## Troubleshooting & Doris Gotchas

| Symptom / Doris Error | Cause | Resolution |
| --- | --- | --- |
| `errCode = 2, replication num 3 is larger than available backends 1` | Doris defaults to `replication_num = 3`, which fails on single-BE local/CI clusters. | Set `replication_num: 1` under `model_defaults.physical_properties` in `config.yaml` for local dev. |
| `PARTITION BY` omitted on OLAP table (logged as a warning) | Doris OLAP tables with `PARTITION BY (...) ()` and no partition creation strategy have zero partitions and reject all `INSERT`s. | Add `auto_partition = 'true'`, `'dynamic_partition.enable' = 'true'`, or explicit `partitions = '...'` to `physical_properties`, or set `partition_by_time_column false` in `INCREMENTAL_BY_TIME_RANGE` if you do not want OLAP partitioning. |
| `Doris only supports creating views in the 'internal' catalog` | External Iceberg catalogs do not support Doris `VIEW` or `MATERIALIZED VIEW` objects. | Set `virtual_environment_mode: dev_only` and `environment_catalog_mapping: {"^(?!prod$).*": "internal"}` in `config.yaml`. |
| `Key column cannot be of type STRING / DOUBLE / ARRAY` | Doris forbids `STRING`/`TEXT`, floating-point (`FLOAT`/`DOUBLE`), and complex types as OLAP key columns. | SQLMesh automatically reorders valid key columns first and coerces `STRING`/`TEXT` key columns to `VARCHAR(255)`. For explicit keys on `DOUBLE`/`FLOAT`, use `DECIMAL` or an integer/date key column. |
