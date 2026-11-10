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
# Local development (with bundled DuckDB state_connection)
pip install "sqlmesh[doris]"

# Production (with a PostgreSQL state_connection)
pip install "sqlmesh[doris,postgres]"
```

### 5-Minute Quickstart

1. **Initialize a new Doris project** (generates `config.yaml` with `dialect: doris` and a local DuckDB `state_connection`):
   ```bash
   mkdir doris_demo && cd doris_demo
   sqlmesh init doris
   ```
2. **Configure connection credentials**: Edit `config.yaml` and fill in the required `host` (e.g., `127.0.0.1`) and `user` (e.g., `root`) fields under `gateways.doris.connection`.
3. **Single-BE Docker / Local Dev Tip**: Doris defaults to `replication_num = 3`. When running against a single-BE Docker container, set `replication_num = 1` in `model_defaults.physical_properties` inside `config.yaml`:
   ```yaml
   model_defaults:
     dialect: doris
     start: 2024-01-01
     physical_properties:
       replication_num: 1
   ```
4. **Plan and apply**:
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
          # Optional: omit on a fresh Doris instance so SQLMesh can create model schemas automatically,
          # or create the database first (CREATE DATABASE IF NOT EXISTS analytics) before setting it here:
          # database: analytics
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
          password: {{ env_var('DORIS_PASSWORD') | tojson }}
          # Must already exist in Doris if specified (or omit to let SQLMesh qualify schemas directly)
          database: analytics
          catalog: internal
          connect_timeout: 30
          ssl_ca: /etc/ssl/certs/doris-ca.pem
          ssl_verify_cert: true
          ssl_verify_identity: true
          session_variables:
            workload_group: etl_group
            query_timeout: 3600
        state_connection:
          type: postgres
          host: state-db.internal
          port: 5432
          user: sqlmesh
          password: {{ env_var('POSTGRES_PASSWORD') | tojson }}
          database: sqlmesh_state
          sslmode: verify-full
          # Set PGSSLROOTCERT=/etc/ssl/certs/postgres-ca.pem in the environment to verify
          # the PostgreSQL server certificate against your PostgreSQL CA bundle (separate from Doris TLS).

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
    -- Custom sort keys via order_by require Doris 4.1+; omit order_by on Doris < 4.1
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
WHERE CAST(updated_at AS DATE) BETWEEN @start_ds AND @end_ds;
```

You can also specify `unique_key` explicitly in `physical_properties` for `FULL` or `INCREMENTAL_BY_TIME_RANGE` models, and on **Doris 4.1+** optionally decouple the sort key from the unique key using `order_by` or `cluster_by` (emitting `ORDER BY (col1, col2)` on Doris Merge-on-Write tables; omit `order_by` on Doris `< 4.1`).

On Doris 4.1+, `INCREMENTAL_BY_UNIQUE_KEY` models also support `when_matched` and `merge_filter` via Doris's native `MERGE INTO` syntax (automatically excluding `unique_key` columns from `WHEN MATCHED THEN UPDATE SET` to satisfy Doris Merge-on-Write value-column constraints):

!!! note "Requirements for `when_matched` and `merge_filter`"
    - Without `when_matched` or `merge_filter`, `INCREMENTAL_BY_UNIQUE_KEY` uses `DELETE` + `INSERT INTO`, which is supported on Doris 2.1+ across all delete-capable table models.
    - When `when_matched` or `merge_filter` is specified, `DorisEngineAdapter` uses native `MERGE INTO`, which requires **Apache Doris 4.1+** and an internal OLAP **`UNIQUE KEY`** target table. Attempting to use `when_matched` or `merge_filter` on Doris `< 4.1`, on a table configured with `duplicate_key` or `aggregate_key` in `physical_properties`, or on an external catalog / lakehouse table raises a `SQLMeshError`.

```sql
MODEL (
  name analytics.orders_upsert,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key order_id,
    when_matched WHEN MATCHED AND source.updated_at > target.updated_at THEN UPDATE SET
      target.status = source.status,
      target.updated_at = source.updated_at
  )
);

SELECT
  order_id::BIGINT AS order_id,
  status::VARCHAR(32) AS status,
  updated_at::DATETIME AS updated_at
FROM raw.orders_changelog
WHERE updated_at BETWEEN @start_ts AND @end_ts;
```

### AGGREGATE KEY model

For pre-aggregated rollup tables, specify the key columns using `aggregate_key` in `physical_properties`. Note that Doris requires non-key value columns in an `AGGREGATE KEY` table to specify an aggregation function (`SUM`, `MAX`, `MIN`, `REPLACE`, etc.) in the column definition unless all columns are part of `aggregate_key` (or the table is created via a custom DDL / pre-statement):

```sql
MODEL (
  name analytics.daily_site_keys,
  kind FULL,
  clustered_by (user_id),
  physical_properties (
    aggregate_key = ('user_id', 'event_type'),
    buckets = 16
  )
);

SELECT
  user_id::BIGINT AS user_id,
  event_type::VARCHAR(64) AS event_type
FROM analytics.user_events
GROUP BY user_id, event_type;
```

---

## Distribution & Bucketing

Doris OLAP tables distribute tablets across backends via `DISTRIBUTED BY HASH(...)` or `DISTRIBUTED BY RANDOM`. Later Doris 2.1.x+ and 3.x/4.x releases allow omitting `DISTRIBUTED BY`—defaulting to `DISTRIBUTED BY RANDOM BUCKETS AUTO` for `DUPLICATE KEY` tables and `AGGREGATE KEY` tables without `REPLACE` or `REPLACE_IF_NOT_NULL` value columns—whereas Doris 2.0 and 2.1.0 require an explicit `DISTRIBUTED BY` clause on schema-defined OLAP tables (so on Doris 2.0 / 2.1.0, always configure distribution explicitly via `clustered_by` or `distributed_by` on non-`UNIQUE KEY` models), and `AGGREGATE KEY` tables with `REPLACE` or `REPLACE_IF_NOT_NULL` columns as well as `UNIQUE KEY` tables reject random distribution and require `DISTRIBUTED BY HASH(...)` in native DDL. SQLMesh supports three ways to configure distribution:

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

   SELECT
     user_id::BIGINT AS user_id,
     event_type::VARCHAR(64) AS event_type
   FROM raw.events;
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

Doris OLAP tables support **Multi-Range Batch Partitioning**, **Explicit Partition Definitions**, **Dynamic Partitioning**, and **Auto Partitioning** (Doris 2.1+). You can specify the partition expression via the top-level `partitioned_by` model property or via `partition_by` (or quoted `'partitioned_by'`) inside `physical_properties`.

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

SELECT
  event_date::DATE AS event_date,
  user_id::BIGINT AS user_id
FROM raw.events
WHERE event_date BETWEEN @start_ds AND @end_ds;
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

SELECT
  event_time::DATETIME AS event_time,
  user_id::BIGINT AS user_id
FROM raw.events
WHERE event_time BETWEEN @start_ts AND @end_ts;
```

### Dynamic partitioning (`dynamic_partition.enable = 'true'`)

For rolling retention windows managed by Doris FE's dynamic partition scheduler, enable `'dynamic_partition.create_history_partition' = 'true'` so Doris creates the historical partitions on table creation, set `'dynamic_partition.time_zone' = 'UTC'` so Doris evaluates partition boundaries in the same UTC timezone that SQLMesh uses to resolve `start`, and set `'dynamic_partition.start'` with a buffer beyond the model's `start` window (for example, `'-32'` for `start '30 days ago'`) so backfills that cross a midnight boundary only insert into existing partitions:

```sql
MODEL (
  name analytics.rolling_daily_events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date
  ),
  start '30 days ago',
  partitioned_by RANGE(event_date),
  physical_properties (
    'dynamic_partition.enable' = 'true',
    'dynamic_partition.time_unit' = 'DAY',
    'dynamic_partition.time_zone' = 'UTC',
    'dynamic_partition.start' = '-32',
    'dynamic_partition.end' = '3',
    'dynamic_partition.create_history_partition' = 'true',
    'dynamic_partition.prefix' = 'p',
    'dynamic_partition.buckets' = '16'
  )
);

SELECT
  event_date::DATE AS event_date,
  user_id::BIGINT AS user_id
FROM raw.events
WHERE event_date BETWEEN @start_ds AND @end_ds;
```

---

## Apache Iceberg & Multi-Catalog Lakehouse Integration

Apache Doris 2.1–5.0 acts as a federated lakehouse engine that can query and write (including `CREATE TABLE`) to external **Apache Iceberg** catalogs backed by REST/Polaris, AWS Glue, Hive Metastore, or Hadoop/S3. Native Alibaba Cloud DLF Iceberg catalogs can be queried as upstream sources, but Doris does not support `CREATE TABLE` in native DLF Iceberg catalogs, so SQLMesh cannot create new model tables there.

### Pattern 1: Reading Iceberg Bronze tables into Doris OLAP Silver/Gold models

Because `DorisEngineAdapter` supports multi-catalog 3-part naming (`catalog.db.table`), your SQLMesh models can query external Iceberg tables directly while materializing sub-second serving tables in Doris's `internal` OLAP catalog:

```sql
MODEL (
  name silver.user_sessions,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column session_date,
    partition_by_time_column false
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

!!! warning "Non-Atomic Iceberg `FULL` Refreshes & Recovery Procedure"
    `DorisEngineAdapter` disables multi-statement transactions (`SUPPORTS_TRANSACTIONS = False`). When refreshing an existing Iceberg table via `DELETE` followed by `INSERT INTO` (for example, during in-place `FULL` refreshes with `virtual_environment_mode: dev_only`), the two steps are **non-atomic**:

    - **Concurrent reader visibility**: Readers querying the Iceberg table between the `DELETE` snapshot(s) and the `INSERT INTO` snapshot may see an empty or partially cleared table.
    - **Insert failure risk**: If the `INSERT INTO` statement fails after the `DELETE` statements commit, the production table remains empty until recovered.
    - **Verified recovery procedures** (quiesce concurrent writers before starting recovery):
      1. **Replace or roll back to the prior Iceberg snapshot (`FOR VERSION AS OF`)**: Because `DorisEngineAdapter` clears Iceberg tables using two separate `DELETE` statements (`IS NOT NULL` and `IS NULL`), a failure after the first `DELETE` can leave residual rows in the table. Do not blindly `INSERT` the prior snapshot on top of a partially cleared table (which would duplicate surviving rows), and do not select the recovery snapshot by `committed_at` timestamp order alone (since `$snapshots` includes commits from other branches). Instead, quiesce writers, trace the `parent_id` chain from the production `main` branch head in `$refs` back through the failed refresh's `delete` snapshot(s) in `$snapshots` to find the first failed-refresh `delete` snapshot's `parent_id` (`<pre_delete_snapshot_id>`), verify that `<pre_delete_snapshot_id>` belongs to the production table's lineage, and either roll back the table metadata to `<pre_delete_snapshot_id>` in your Iceberg catalog or clear any remaining rows before restoring that snapshot:
         ```sql
         -- 1. Inspect the production 'main' branch head and follow parent_id through the failed refresh's
         --    'delete' snapshot(s) to identify the first failed-refresh delete snapshot's parent_id (<pre_delete_snapshot_id>):
         SELECT name, type, snapshot_id
         FROM iceberg_catalog.silver.curated_events$refs
         WHERE name = 'main';

         SELECT snapshot_id, parent_id, committed_at, operation
         FROM iceberg_catalog.silver.curated_events$snapshots;

         -- 2. After quiescing writers, clear any surviving rows from a partial delete so the restore replaces rather than appends:
         DELETE FROM iceberg_catalog.silver.curated_events WHERE user_id IS NOT NULL;
         DELETE FROM iceberg_catalog.silver.curated_events WHERE user_id IS NULL;

         -- 3. Restore the pre-delete state from the prior snapshot using Iceberg time travel:
         INSERT INTO iceberg_catalog.silver.curated_events
         SELECT * FROM iceberg_catalog.silver.curated_events FOR VERSION AS OF <pre_delete_snapshot_id>;
         ```
      2. **Restate the model or retry the failed plan in SQLMesh**: Once writers are quiesced and the cause of the failure is resolved, run `sqlmesh plan --restate-model iceberg_catalog.silver.curated_events` (or retry the failed `sqlmesh plan`) to replace all rows from the upstream source. Do not rely on `sqlmesh run` alone for recovery, because `sqlmesh run` may skip an interval that was already recorded as processed and leave the table empty.

```sql
-- On initial creation, SQLMesh runs CREATE TABLE ... ENGINE=iceberg followed by INSERT INTO.
-- On subsequent FULL refreshes, SQLMesh clears all existing rows via non-atomic DELETE + INSERT INTO
-- (rather than dynamic-partition INSERT OVERWRITE) so omitted partitions and empty results
-- replace the entire Iceberg v2 table cleanly; see the warning above for reader visibility and recovery.
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

SQLMesh's Doris dialect natively parses and transpiles Apache Iceberg snapshot time travel, branch/tag reads and branch/tag DDL (requires Doris 3.1+), and table maintenance actions such as `rewrite_data_files` compaction (requires Doris 4.0.2+) and `expire_snapshots` snapshot expiration (requires Doris 4.1.0+) while preserving upstream table lineage:

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
  You can run Iceberg file compaction (`rewrite_data_files`, requires Doris 4.0.2+), snapshot expiration (`expire_snapshots`, requires Doris 4.1.0+ with ISO-8601 `yyyy-MM-ddTHH:mm:ss` or epoch-millisecond `older_than`), or branch/tag management (requires Doris 3.1+) in model post-statements. Because Doris does not support Iceberg orphan-file cleanup (`remove_orphan_files`), run orphan-file cleanup through a supported external Iceberg maintenance tool (such as Apache Spark's `remove_orphan_files` procedure):
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

### Pattern 4: Reading & Writing External Apache Hive and Apache Paimon Catalogs

In addition to Apache Iceberg, `DorisEngineAdapter` supports reading and writing external **Apache Hive** (`'type' = 'hms'`, `table_format hive`) catalogs and reading external **Apache Paimon** (`'type' = 'paimon'`, `table_format paimon`) catalogs:

- **Apache Hive (`table_format hive`)**: Supported for reading (`SELECT` on Doris 2.1+) and writing (`CREATE TABLE ... ENGINE=hive`, `INSERT INTO`, and `INSERT OVERWRITE TABLE` on Doris 2.1.6+). Emits `ENGINE=hive` and maps `storage_format` to `'file_format'` (`'parquet'`, `'orc'`, or `'text'`). Supports `FULL` (`INSERT OVERWRITE TABLE` on unpartitioned Hive tables; partitioned Hive `FULL` refreshes are not supported until full-table replacement is available because Doris `INSERT OVERWRITE` only replaces partitions present in the query result and Hive catalogs in Doris do not support `DELETE`) and append workloads (`INCREMENTAL_UNMANAGED`), while row-level `DELETE` and `MERGE INTO` are not supported by Hive catalogs in Doris. Just like external Iceberg targets, because Doris only supports `CREATE VIEW` in the `internal` catalog, materializing models inside an external Hive catalog requires configuring `virtual_environment_mode: dev_only`, `environment_suffix_target: schema` (or `table`), and `environment_catalog_mapping: {"^(?!prod$).*": "internal"}` in `config.yaml` (see the virtual environment configuration in [Pattern 2](#pattern-2-materializing-apache-iceberg-tables-via-doris)).
- **Apache Paimon (`table_format paimon`)**: Officially supported in Doris 2.1–4.1.5 for reading upstream Paimon catalog tables (`SELECT`). While `DorisEngineAdapter` also generates `ENGINE=paimon` DDL (`'file.format'` and `'primary-key'`) for builds that include experimental Paimon write support (tracked upstream in `apache/doris#65086`), Doris 2.1–4.1.5 does not ship general-availability Paimon `INSERT` / `DELETE` / `MERGE INTO` support—use **Apache Iceberg v2** (`table_format iceberg` on Doris 4.1.0+) when your lakehouse target requires `DELETE` or `MERGE INTO`.

```sql
-- Requires virtual_environment_mode: dev_only, environment_suffix_target: schema (or table),
-- and environment_catalog_mapping: {"^(?!prod$).*": "internal"} in config.yaml because Doris
-- only supports CREATE VIEW inside the internal catalog.
MODEL (
  name hive_catalog.silver.orders_hive,
  kind FULL,
  table_format hive,
  storage_format parquet
);

SELECT
  order_id::BIGINT AS order_id,
  amount::DECIMAL(18, 2) AS amount,
  ds::DATE AS ds
FROM paimon_catalog.raw.orders;
```

### Pattern 5: Reading External Lance, Hudi & Delta Lake Tables (Doris 4.1+)

Apache Doris 4.1.4+ supports reading multimodal and vector datasets stored in **Lance** format (`'type' = 'lance'` catalog or `s3(...)` / `local(...)` table-valued functions with `'format' = 'lance'`), as well as external **Apache Hudi** (via a Hive Metastore catalog with `'type' = 'hms'`) and **Delta Lake** (`'type' = 'deltalake'`) catalogs.

!!! warning "Do Not Put Credentials in `before_all`"
    SQLMesh renders `before_all` statements and persists them in the `_environment_statements` state table. Provision external catalogs that require static `s3.access_key` / `s3.secret_key` credentials directly in Doris out-of-band, or configure `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the Doris FE and BE process environments (`'s3.credentials_provider_type' = 'ENV'`, which both `S3Properties` and Lance's `StorageOptions::with_env_s3` resolve automatically in Doris 4.1.4+ without storing secrets in `before_all`; note that Doris 4.1.4's `LanceS3StorageProvider` does not forward `s3.role_arn` to Lance).

```yaml
# config.yaml
before_all:
  - >
    CREATE CATALOG IF NOT EXISTS lance_catalog PROPERTIES (
      'type' = 'lance',
      'lance.catalog.type' = 'filesystem',
      'warehouse' = 's3://ai-lakehouse/lance_warehouse',
      's3.endpoint' = 'https://ai-lakehouse.s3.us-east-1.amazonaws.com',
      's3.region' = 'us-east-1',
      's3.credentials_provider_type' = 'ENV'
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
    -- Custom sort keys via order_by require Doris 4.1+; omit order_by on Doris < 4.1
    order_by = ('updated_at', 'order_id'),
    buckets = 16,
    enable_unique_key_merge_on_write = 'true',
    light_schema_change = 'true'
  )
);

SELECT
  CAST(NULL AS BIGINT) AS order_id,
  CAST(NULL AS BIGINT) AS user_id,
  CAST(NULL AS DECIMAL(18, 2)) AS amount,
  CAST(NULL AS DATETIME) AS updated_at;
```

!!! tip "Zero-Downtime Schema Evolution with Flink / Routine Load"
    Setting `forward_only true` and `disable_restatement true` on a `doris_ddl_only` model ensures that schema additions (`ALTER TABLE ... ADD COLUMN`) are applied in-place on the existing table (`light_schema_change = 'true'`) rather than creating a new empty physical table, and prevents accidental restatement wipes.

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

SELECT
  event_id::BIGINT AS event_id,
  payload::STRING AS payload
FROM raw.cloud_events;
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
  physical_properties (
    build = 'IMMEDIATE',
    refresh = 'AUTO',
    refresh_trigger = 'ON SCHEDULE EVERY 1 HOUR',
    distributed_by = 'HASH(user_id) BUCKETS 10'
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
    - When `build = 'DEFERRED'` is used on an asynchronous MTMV that also defines `audits (...)`, SQLMesh automatically submits `REFRESH MATERIALIZED VIEW <mv> AUTO` after creation. Because Doris executes MTMV refresh tasks asynchronously without blocking `REFRESH MATERIALIZED VIEW`, audits may run before the background refresh task finishes or fails; use `build = 'IMMEDIATE'` when audits require the initial refresh to complete before auditing.
    - When `excluded_trigger_tables` (supports a single string, comma-separated string, or tuple `('db.t1', 'db.t2')` on async MTMVs) references a SQLMesh-managed model, SQLMesh automatically resolves the model name to its underlying physical snapshot table name.

### Synchronous Materialized Views (Base-Table Rollup Indexes)

Doris synchronous materialized views are single-table rollup indexes maintained atomically on a single `internal`-catalog OLAP base table. Unlike asynchronous MTMVs, Doris does **not** allow synchronous materialized views to be queried directly by name (`SELECT * FROM <sync_mv>`)—instead, the Doris optimizer transparently routes matching queries against the base table to the synchronous rollup index.

Because SQLMesh `VIEW (materialized true)` models are promoted via a virtual view (`SELECT * FROM <physical_mv>`) and queried directly by downstream models, **SQLMesh `VIEW (materialized true)` models must use asynchronous materialized views (MTMVs)**. To attach a synchronous materialized view to a SQLMesh-managed OLAP base table, create it as an index on `@this_model` in a post-statement (while `DorisEngineAdapter.create_view(..., materialized=True, view_properties={"materialized_type": exp.Literal.string("SYNC"), "source_table": ...})` remains available for direct programmatic DDL management):

```sql
MODEL (
  name analytics.user_events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_time,
    partition_by_time_column false
  ),
  grain (user_id, event_time)
);

SELECT
  user_id::BIGINT AS user_id,
  event_type::VARCHAR(64) AS event_type,
  event_time::DATETIME AS event_time
FROM raw.user_events
WHERE event_time BETWEEN @start_ts AND @end_ts;

-- Note: For INCREMENTAL_BY_TIME_RANGE models, every synchronous MV (rollup) on the
-- base table must include the time_column (event_time) in its SELECT and GROUP BY
-- keys so Doris permits DELETE WHERE event_time ... during interval replacement.
-- For rollups that aggregate across all event_time values, use an asynchronous MTMV instead.
@IF(
  @runtime_stage = 'creating',
  CREATE MATERIALIZED VIEW user_events_sync_mv AS
  SELECT
    user_id AS mv_user_id,
    event_time AS mv_event_time,
    COUNT(event_type) AS event_count
  FROM @this_model
  GROUP BY user_id, event_time
);
```

### Inverted & Vector Indexes, Full-Text Search & Compaction (Doris 2.1–5.0)

You can attach Doris inverted indexes (`USING INVERTED` with tokenizer `PROPERTIES`), bloom filter indexes (`USING NGRAM_BF`), or Doris 4.x+ ANN vector indexes (`USING ANN` with `hnsw` or `ivf` index types; ANN indexes on `UNIQUE KEY` tables require Doris 4.1.4+ with Merge-on-Write enabled, whereas earlier Doris 4.x versions only support ANN indexes on `DUPLICATE KEY` tables) using post-statements. Because Doris requires ANN vector index columns to be `ARRAY<FLOAT> NOT NULL` at table creation time (and Doris ignores `NON_NULLABLE(...)` on non-`AUTO PARTITION` CTAS columns while forbidding `ALTER TABLE ... MODIFY COLUMN` from changing an existing nullable column to `NOT NULL`), declare `not_null_columns = ('embedding')` in `physical_properties` so SQLMesh materializes the table schema with `embedding ARRAY<FLOAT> NOT NULL` before inserting rows.

When SQLMesh pre-creates an empty physical snapshot table during the snapshot-creation step (`create_snapshot`), `@IF(@runtime_stage = 'creating', ...)` post-statements run before data is loaded, so `ALTER TABLE ... ADD INDEX` already indexes the initial load. However, when a missing target table is created **during evaluation**—whether via lazy creation (unresolved column types) or evaluation-time table creation on a missing target table (even with known column types)—SQLMesh inserts the first batch **before** running `@IF(@runtime_stage = 'creating', ...)` post-statements, so `ALTER TABLE ... ADD INDEX` for `INVERTED` or `ANN` indexes only registers the index for subsequent writes:

- **Compute-storage integrated deployments**:
  - **`USING INVERTED` and `USING ANN` indexes**: Follow `ALTER TABLE @this_model ADD INDEX <index_name> ...` with `BUILD INDEX <index_name> ON @this_model` inside `@runtime_stage = 'creating'` so any pre-existing rows inserted before the post-statement are backfilled (on an empty table, `BUILD INDEX` is a fast no-op). `DorisEngineAdapter` polls `SHOW ALTER TABLE COLUMN` and `SHOW BUILD INDEX` for up to 60 seconds, but returns early on timeout, `CANCELLED` jobs, or polling errors—verify index readiness via `SHOW ALTER TABLE COLUMN` / `SHOW BUILD INDEX` when downstream models depend on index builds completing first.
  - **`USING NGRAM_BF` bloom filter indexes**: Do **not** run `BUILD INDEX`—Doris rejects `BUILD INDEX` for `NGRAM_BF` indexes. When `enable_add_index_for_new_data = false` (so Doris uses the heavy `SCHEMA_CHANGE` path rather than light index change), `ALTER TABLE @this_model ADD INDEX <index_name> (<col>) USING NGRAM_BF PROPERTIES(...)` rebuilds the filter across existing and new rows via an asynchronous `SCHEMA_CHANGE` job (monitor via `SHOW ALTER TABLE COLUMN`). If `enable_add_index_for_new_data = true` with light index changes enabled, `ADD INDEX` registers the filter only for new writes, so `SHOW ALTER TABLE COLUMN` reporting `FINISHED` alone does not confirm historical-row backfill—use the heavy `SCHEMA_CHANGE` path (`enable_add_index_for_new_data = false`) when existing rows must be indexed.
- **Compute-storage decoupled deployments (`storage_vault_name`)**: Doris 4.x does not support `BUILD INDEX` in compute-storage decoupled mode (executing `BUILD INDEX` will fail); omit `BUILD INDEX`. Because Doris 4.1.5 may rebuild existing data through a `SCHEMA_CHANGE` job when `enable_add_index_for_new_data` is `false`, check the active index-change mode (`enable_add_index_for_new_data`) and `SHOW ALTER TABLE COLUMN` job status before running any manual rewrite or restatement so you avoid unnecessary reprocessing. Only if existing rows from evaluation-time creation remain unindexed, choose the backfill strategy matching the model kind:
  - **`FULL`, `INCREMENTAL_BY_TIME_RANGE`, and `INCREMENTAL_BY_UNIQUE_KEY`**: Run `sqlmesh plan --restate-model <model>` (provided the upstream source still retains the restated interval/rows) to rewrite rows through the newly registered index.
  - **`INCREMENTAL_BY_PARTITION`**: Warning—`sqlmesh plan --restate-model <model>` drops and rebuilds the **entire** table, which loses historical partitions unless the upstream source still reproduces the full partition history; otherwise stop all writers to the table before starting the overwrite, rewrite only the unindexed partition(s) in place via `INSERT OVERWRITE TABLE <physical_table> PARTITION(...) SELECT ...`, and keep all writers stopped until the overwrite completes so concurrent appends are not lost.
  - **`INCREMENTAL_UNMANAGED`**: Restatement is disabled by default (`disable_restatement = true`), so `--restate-model` skips the model unless `disable_restatement false` is explicitly set (which truncates and reloads the full table). Choose the backfill path based on whether currently stored `UNIQUE KEY` delete/sequence tombstones (`__DORIS_DELETE_SIGN__`) must be preserved across the backfill operation:
    - **Tables where `UNIQUE KEY` sequence tombstones (`__DORIS_DELETE_SIGN__`) do not need to be retained**: To preserve live rows without truncating from source, stop all writers to the table before starting the overwrite, ensure `SET show_hidden_columns = false;` is set in the session (or explicitly filter `WHERE __DORIS_DELETE_SIGN__ = 0` in the `SELECT`, because `show_hidden_columns = true` disables Doris's automatic delete-sign filter and an `INSERT OVERWRITE` omitting `__DORIS_DELETE_SIGN__` would reinsert deleted rows as live rows), rewrite the existing live rows in place on the physical snapshot table after `SHOW ALTER TABLE COLUMN` reports `FINISHED`—listing columns explicitly and including `__DORIS_SEQUENCE_COL__` on `UNIQUE KEY` tables configured with `function_column.sequence_type` so hidden sequence values on live rows are preserved (`INSERT OVERWRITE TABLE <physical_table> (<col1>, <col2>, ..., __DORIS_SEQUENCE_COL__) SELECT <col1>, <col2>, ..., __DORIS_SEQUENCE_COL__ FROM <physical_table>`, or omitting `__DORIS_SEQUENCE_COL__` when `function_column.sequence_type` is not used)—and keep all writers stopped until the overwrite completes so concurrent appends are not lost.
    - **Tables where stored `UNIQUE KEY` sequence tombstones (`__DORIS_DELETE_SIGN__`) must not be stripped by the backfill**: Do **not** use self-`INSERT OVERWRITE` (with `show_hidden_columns = false`, `SELECT` excludes deleted rows and immediately discards their tombstones, allowing an older out-of-order write to resurrect a deleted key). Note that the procedures below only avoid stripping tombstones **during the index rebuild or replay itself**—on Doris 4.1.5 Merge-on-Write (`enable_unique_key_merge_on_write = 'true'`) tables without cluster keys (`order_by`), subsequent base compaction can still prune `__DORIS_DELETE_SIGN__` tombstones later, so ongoing protection against late out-of-order writes requires a verified Doris compaction/tombstone-retention strategy and neither procedure below is sufficient on its own without one:
      1. **Build or rebuild the index via heavy `SCHEMA_CHANGE` in compute-storage decoupled mode (preserves existing segments, sequence values, and currently stored tombstones during the schema change)**: First verify `SHOW BUILD INDEX FROM <db> WHERE TableName = '<table_name>'` has no active (`WAITING_TXN` or `RUNNING`) index-change jobs on the table (Doris rejects starting a `SCHEMA_CHANGE` job while any `IndexChangeJob` is unfinished). If the index is **not yet registered** on `<physical_table>`, run `SET enable_add_index_for_new_data = false;` followed by `ALTER TABLE <physical_table> ADD INDEX <index_name> ...`, then wait until the resulting job in `SHOW ALTER TABLE COLUMN FROM <db> WHERE TableName = '<table_name>'` reaches `State = 'FINISHED'` (and verify it did not end in `CANCELLED`). If the index is **already registered** (for example, added during `@runtime_stage = 'creating'` with `enable_add_index_for_new_data = true`), setting `enable_add_index_for_new_data = false` alone will not rebuild it because Doris rejects a repeated `ADD INDEX` (or skips it as a no-op with `IF NOT EXISTS`): run `SET enable_add_index_for_new_data = false;`, drop the index via `ALTER TABLE <physical_table> DROP INDEX <index_name>`, wait until the drop's `SHOW ALTER TABLE COLUMN FROM <db> WHERE TableName = '<table_name>'` job reaches `State = 'FINISHED'` (not `CANCELLED`), `SHOW BUILD INDEX FROM <db> WHERE TableName = '<table_name>'` has no active (`WAITING_TXN` or `RUNNING`) jobs, and `SHOW INDEX FROM <physical_table>` no longer lists `<index_name>`, then recreate the index in the same `enable_add_index_for_new_data = false` session via `ALTER TABLE <physical_table> ADD INDEX <index_name> ...` and wait until the new `SHOW ALTER TABLE COLUMN FROM <db> WHERE TableName = '<table_name>'` job reaches `State = 'FINISHED'` (not `CANCELLED`).
      2. **Replay into a deployable SQLMesh snapshot from an authoritative changelog/CDC source (`forward_only false` with `virtual_environment_mode: full`)** (prefer option 1 when possible, and replay only after verifying the upstream changelog/CDC source retains the required history of live rows, deleted-key tombstones, and sequence values): `INCREMENTAL_UNMANAGED` models default to `forward_only = true`, under which a `stamp` bump in `sqlmesh plan <dev_env>` evaluates into a separate non-deployable `__temp` preview table while `prod` retains its existing physical table on promotion (`prod` will **not** serve that `dev` table). Use this `dev`-snapshot promotion workflow **only** when the snapshot is deployable so `prod` will target the staged physical table—configure `kind INCREMENTAL_UNMANAGED (forward_only false)` under `virtual_environment_mode: full` (with no pinned `physical_version`, no `auto_restatement_cron`, no non-deployable upstream snapshots, and a `NON_BREAKING` or `BREAKING` `stamp` bump rather than `--forward-only` or a metadata-only plan; prefer `NON_BREAKING` when downstream schemas/semantics are unchanged so downstream models remain `INDIRECT_NON_BREAKING`). First, **pause and drain all scheduled or ad-hoc `sqlmesh run` / `sqlmesh plan` jobs and all external CDC/ingestion writers, keeping every SQLMesh and external writer paused throughout staging, replay, promotion, and writer redirection—no writes to the old physical table may occur during replay or promotion**. After all writers are drained, **capture a durable upstream changelog/CDC source offset (`<cutover_offset>`)** so no CDC events committed before writers finished pausing can be omitted, and set `<cutover_ts>` to an **explicit timestamp including time and timezone** (for example, `'2025-01-10 00:00:00+00:00'`—**never** a date-only value such as `'2025-01-10'`, which `--empty-backfill` can treat as inclusive of that full day and record through the next day when no prior non-seed interval ends exist) equal to the latest cron- and partition-aligned boundary **fully covered** by `<cutover_offset>` (at least one full model interval past `start` and accounting for any model `lookback`, catching up any upstream SQLMesh models through `<cutover_ts>` before running `--empty-backfill`; if `<cutover_offset>` includes partial-interval CDC events beyond `<cutover_ts>` inside `[<cutover_ts>, <next_boundary_ts>)` on an `insert_overwrite true` model whose SQLMesh query does not emit tombstones or other CDC-only rows, note that waiting for resumed CDC writers to advance past that open partition does **not** record `[<cutover_ts>, <next_boundary_ts>)` in SQLMesh state—either drain writers at a cron- and partition-aligned boundary so `<cutover_offset>` aligns with `<cutover_ts>`, or after promoting and resuming external CDC writers keep `sqlmesh run` paused until external CDC writers advance past explicit timestamp `<next_boundary_ts>` with time and timezone, verify that completed interval's live rows, tombstones, and sequence values on the promoted `prod` table, and reconcile that completed interval in SQLMesh state via `sqlmesh plan --forward-only --select-model <model> --backfill-model <model> --empty-backfill --execution-time <next_boundary_ts>` without `--run`, bumping `stamp` if needed so the forward-only update applies on the existing `prod` physical table version without creating a new table and re-verifying `SHOW CREATE VIEW <prod_view>`, before resuming `sqlmesh run`). Stage the empty deployable table without running model SQL batches via `sqlmesh plan <dev_env> --select-model <model> --backfill-model <model> --empty-backfill --execution-time <cutover_ts>` (passing `<cutover_ts>` with an explicit time and timezone and `enable_add_index_for_new_data: false` in `session_variables`; `--empty-backfill` creates the empty deployable table and runs `@runtime_stage = 'creating'` index post-statements without executing model batches while recording deployable intervals only through `<cutover_ts>` so the first post-cutover `sqlmesh run` does not treat the table as a first insert and replace it), and **verify before replaying that `SHOW CREATE VIEW <dev_view>` points to a new deployable physical snapshot table (without a `__temp` suffix) distinct from `SHOW CREATE VIEW <prod_view>`** while `<prod_view>` keeps serving readers from the untouched old physical table. Replay **both** live rows and deleted keys' tombstones (`__DORIS_DELETE_SIGN__ = 1`) together with their sequence values (`__DORIS_SEQUENCE_COL__` or the configured `function_column.sequence_col` column) from the upstream source **up to that `<cutover_offset>`** into the staged deployable physical table (reloading live rows alone discards tombstones and is not sufficient), and validate row/tombstone counts and `SHOW INDEX` on the staged table. If replay fails, `prod` remains untouched, **do not promote that staged snapshot**—because `--empty-backfill` already recorded deployable intervals on that snapshot version in state and dropping `<dev_env>` alone does **not** clear the deployable snapshot, its recorded intervals, or its incomplete physical table, **before running another `prod` plan** require either bumping `stamp` to create a new snapshot version or reverting the cutover `stamp`/`forward_only` edit **and** verified cleanup of the failed snapshot, its recorded intervals, and its incomplete staged table (`sqlmesh invalidate <dev_env> --sync` followed by `sqlmesh janitor --ignore-ttl`, confirming the incomplete staged table and its snapshot/interval state are removed) so promotion cannot reuse the incomplete table as already processed. Once replay validation succeeds, promote to `prod` using `sqlmesh plan --select-model <model> --skip-backfill --execution-time <cutover_ts>` without `--run` (verify with `--explain` first that both physical-layer updates and model batches report `SKIP`—`--skip-backfill` prevents a `prod` plan backfill from evaluating any interval that became due after `<dev_env>`, which with `insert_overwrite true` would delete and replace matching partitions in the validated staged table and with `insert_overwrite false` would append model-query rows onto the replay, while the deployable intervals recorded through `<cutover_ts>` during `--empty-backfill` remain in state), **verify `SHOW CREATE VIEW <prod_view>` now targets the newly promoted physical snapshot table**, and—because repointing `<prod_view>` does **not** change the destination of external CDC/ingestion writers that target a physical snapshot table directly—**explicitly reconfigure/redirect all external writers from the old physical table to the newly promoted `prod` physical table starting from that same `<cutover_offset>` and verify both their destination table and `<cutover_offset>` resume position before resuming external writers and (once any open partition is reconciled) scheduled `sqlmesh run` jobs** (if you later remove `forward_only false` to restore default `forward_only = true`, apply that metadata-only plan without `--run` and re-verify `SHOW CREATE VIEW <prod_view>` still targets the promoted physical table).
      3. **Replay on a forward-only `INCREMENTAL_UNMANAGED` model (`forward_only = true`, the default) or non-deployable environment** (prefer option 1 when possible, and replay only after verifying the upstream changelog/CDC source retains the required history of live rows, deleted-key tombstones, and sequence values): Either temporarily make the cutover snapshot deployable (`forward_only false` + `stamp` bump under `virtual_environment_mode: full`) and follow item 2 above so all SQLMesh and external writers stay paused, replay and writer resumption share the captured `<cutover_offset>`, `prod` promotes via `--skip-backfill` without overwriting staged data, and external writers are redirected and verified before resuming, **or**—when keeping `forward_only = true` in place or when `prod` cannot promote a separate deployable snapshot table (note that Doris `ALTER TABLE ... REPLACE WITH TABLE` is not supported in compute-storage decoupled mode)—execute an **in-place maintenance-window cutover** directly on the retained `prod` `<physical_table>` with full **reader and writer** isolation for the entire operation: stop all readers and **pause and drain all scheduled or ad-hoc `sqlmesh run` / `sqlmesh plan` jobs and all external writers** first (keeping every SQLMesh and external writer paused throughout the entire cutover), then **capture the drained external writers' durable upstream changelog/CDC source offset (`<cutover_offset>`)** so replay and writer resumption use the same boundary. Create and verify a recoverable full copy of `<physical_table>` (preserving hidden `__DORIS_DELETE_SIGN__` tombstones and sequence values) before truncating, run `TRUNCATE TABLE <physical_table>`, replay **both** live rows and `__DORIS_DELETE_SIGN__ = 1` tombstones together with their sequence values from the upstream source **up to `<cutover_offset>`** into `<physical_table>`, and validate row/tombstone counts and `SHOW INDEX` on `<physical_table>`. If replay fails, restore `<physical_table>` in place (including tombstones and sequence values) from that verified recovery copy—or, if in-place restore fails, promote a deployable recovery snapshot via item 2 and redirect external CDC writers to that promoted physical table at `<cutover_offset>`—and validate counts, indexes, `<prod_view>` (when virtual views are used), and external CDC writer target destinations before reopening reader or writer access. After a successful in-place replay, **reconcile SQLMesh's recorded snapshot intervals with the replay boundary before resuming any SQLMesh jobs**—otherwise, if `<physical_table>` was initially created with `--skip-backfill` (leaving recorded intervals empty) or its recorded intervals do not yet cover the replay, the next `sqlmesh run` can treat the populated table as a first insert (`is_first_insert = true`) and truncate/replace it (or re-evaluate already-replayed intervals): set `<cutover_ts>` to an **explicit timestamp including time and timezone** (for example, `'2025-01-10 00:00:00+00:00'`—**never** a date-only value such as `'2025-01-10'`) equal to the latest cron- and partition-aligned boundary **fully covered** by `<cutover_offset>` (record **only** intervals completely covered by the replay—never intervals beyond `<cutover_offset>`, accounting for any model `lookback` and catching up upstream SQLMesh models through `<cutover_ts>` first; if the replay covers less than one full model interval from `start`, keep `sqlmesh run` disabled until a full interval is covered and reconciled), and record those covered intervals without running model SQL batches via `sqlmesh plan --forward-only --select-model <model> --backfill-model <model> --empty-backfill --execution-time <cutover_ts>` without `--run` (passing `<cutover_ts>` with an explicit time and timezone, and including a `stamp` bump on `<model>` if needed so `sqlmesh plan` has a forward-only change to apply rather than returning early as a no-op). If `<cutover_offset>` includes partial-interval CDC events beyond `<cutover_ts>` inside `[<cutover_ts>, <next_boundary_ts>)` on an `insert_overwrite true` model whose SQLMesh query does not emit tombstones or other CDC-only rows, waiting for resumed CDC writers to advance past that open partition does **not** record `[<cutover_ts>, <next_boundary_ts>)` in SQLMesh state—either drain writers at a cron- and partition-aligned boundary so `<cutover_offset>` aligns with `<cutover_ts>`, or after resuming external CDC writers keep `sqlmesh run` paused until external CDC writers advance past explicit timestamp `<next_boundary_ts>` (including time and timezone), verify that completed interval's live rows, tombstones, and sequence values on `<physical_table>`, and record that completed interval via `sqlmesh plan --forward-only --select-model <model> --backfill-model <model> --empty-backfill --execution-time <next_boundary_ts>` without `--run` (bumping `stamp` if needed) **before** resuming `sqlmesh run` so the next run cannot evaluate that interval and replace rows missing from the model query. Finally, verify `<prod_view>` (when virtual views are used) targets `<physical_table>` and verify all external CDC writers' destination points to `<physical_table>` (redirecting and verifying any writer whose target was changed during maintenance, never leaving writers pointed at a backup or superseded table) and is positioned at `<cutover_offset>` before resuming readers, external writers, and (once any open partition is reconciled) scheduled `sqlmesh run` jobs.

You can also configure OLAP `compaction_policy` (`'time_series'` or `'size_based'`) and `function_column.sequence_col` in `physical_properties`:

```sql
MODEL (
  name analytics.searchable_logs,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key log_id
  ),
  physical_properties (
    -- Required for ANN vector indexes on UNIQUE KEY tables (requires Doris 4.1.4+)
    enable_unique_key_merge_on_write = 'true',
    -- Define embedding as ARRAY<FLOAT> NOT NULL at CREATE TABLE time (required by Doris ANN indexes;
    -- Doris does not allow ALTER TABLE ... MODIFY COLUMN to change a nullable column to NOT NULL)
    not_null_columns = ('embedding'),
    -- Optional Doris 4.1+ Segment V3 format for wide tables
    storage_format = 'V3',
    -- Use 'size_based' on Merge-on-Write UNIQUE KEY tables ('time_series' is supported on DUPLICATE KEY append/log tables)
    compaction_policy = 'size_based',
    -- Sequence column prevents out-of-order updates with lower sequence values from overwriting newer rows
    'function_column.sequence_col' = 'updated_at'
  )
);

SELECT
  log_id::BIGINT AS log_id,
  message::STRING AS message,
  embedding::ARRAY<FLOAT> AS embedding,
  updated_at::DATETIME AS updated_at,
  created_at::DATETIME(6) AS created_at
FROM raw.logs;

-- DorisEngineAdapter polls SHOW ALTER TABLE COLUMN and SHOW BUILD INDEX for up to 60s
-- (verify status via SHOW ALTER TABLE COLUMN / SHOW BUILD INDEX when index readiness matters).
@IF(
  @runtime_stage = 'creating',
  ALTER TABLE @this_model ADD INDEX idx_message (message) USING INVERTED PROPERTIES ("parser" = "english", "support_phrase" = "true") COMMENT 'Full-text inverted index'
);

@IF(
  @runtime_stage = 'creating',
  BUILD INDEX idx_message ON @this_model
);

@IF(
  @runtime_stage = 'creating',
  ALTER TABLE @this_model ADD INDEX idx_embedding (embedding) USING ANN PROPERTIES ("index_type" = "hnsw", "metric_type" = "l2_distance", "dim" = "3") COMMENT 'ANN vector index'
);

@IF(
  @runtime_stage = 'creating',
  BUILD INDEX idx_embedding ON @this_model
);
```

!!! note "Recovering from Failed Index Post-Statements & Forward-Only Migrations"
    - **Recovering if an `@runtime_stage = 'creating'` index statement fails**: Because `SUPPORTS_TRANSACTIONS = False`, if `CREATE TABLE` (and earlier `ALTER TABLE` post-statements such as `ADD INDEX idx_message`) succeed but a later post-statement such as `ADD INDEX idx_embedding` fails, the partially configured physical snapshot table (`sqlmesh__analytics.analytics__searchable_logs__<version>`) remains in Doris. Retrying `sqlmesh plan` while that incomplete physical table exists can skip table creation (leaving `idx_embedding` missing) or fail on already-created indexes. Before retrying `sqlmesh plan`, either:
      1. Drop the incomplete physical snapshot table (`DROP TABLE IF EXISTS sqlmesh__analytics.analytics__searchable_logs__<version>;`) so the retry recreates the table and runs all `@runtime_stage = 'creating'` statements from scratch, or
      2. Inspect existing indexes (`SHOW INDEX FROM sqlmesh__analytics.analytics__searchable_logs__<version>;`) and manually reconcile the missing `idx_embedding` index (`ALTER TABLE sqlmesh__analytics.analytics__searchable_logs__<version> ADD INDEX idx_embedding ...`) before retrying.
    - **Adding indexes during forward-only migrations**: When SQLMesh creates a new physical snapshot table during a standard `sqlmesh plan`, `@runtime_stage = 'creating'` runs directly on the new physical table. However, during a **forward-only migration** (`sqlmesh plan --forward-only` or `forward_only true`) on an already-existing table, SQLMesh executes `@runtime_stage = 'creating'` against a temporary schema-diff table (`__schema_tmp`) and only applies column-level `ALTER TABLE` diffs to the existing production table—so `ADD INDEX` post-statements in `@runtime_stage = 'creating'` are not applied to the existing production table.

    To add an inverted or ANN index to an existing production table during a forward-only migration:

    1. Resolve the underlying physical snapshot table backing the production view (`SHOW CREATE VIEW analytics.searchable_logs`, e.g., `sqlmesh__analytics.analytics__searchable_logs__<version>`).
    2. Apply `ALTER TABLE` directly on that physical table and wait for `SHOW ALTER TABLE COLUMN` to report `FINISHED`:
       ```sql
       ALTER TABLE sqlmesh__analytics.analytics__searchable_logs__<version>
         ADD INDEX idx_message (message) USING INVERTED
         PROPERTIES ("parser" = "english", "support_phrase" = "true");
       ```
    3. Backfill the index for existing rows:
       - **Compute-storage integrated deployments**: Run `BUILD INDEX idx_message ON sqlmesh__analytics.analytics__searchable_logs__<version>;` and wait for `SHOW BUILD INDEX` to finish.
       - **Compute-storage decoupled (`storage_vault_name`) deployments**: Because Doris does not support `BUILD INDEX` in compute-storage decoupled mode, run a standard non-forward-only `sqlmesh plan` (or `sqlmesh plan --restate-model analytics.searchable_logs` after recreating the table) so all rows are written into the newly indexed table.

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

SELECT
  order_id::BIGINT AS order_id,
  customer_id::BIGINT AS customer_id,
  order_date::DATE AS order_date
FROM raw.orders
WHERE order_date BETWEEN @start_ds AND @end_ds;
```

- **Constant folding in `LIMIT` clauses**: Doris requires `LIMIT` arguments to be integer literals rather than arithmetic expressions (such as `LIMIT 10 + 1` emitted by `number_of_rows`). `DorisEngineAdapter` automatically folds constant arithmetic in `LIMIT` clauses before execution.
- **Deferred Materialized Views (`build = 'DEFERRED'`)**: When a materialized view is created with `build = 'DEFERRED'` and has `audits (...)` attached, SQLMesh automatically triggers `REFRESH MATERIALIZED VIEW ... AUTO` before running audits so they evaluate against populated data.
- **`sqlmesh table_diff` on floating-point and string columns**: Temporary diff tables created by `sqlmesh table_diff` automatically promote integer indicator columns into the Doris OLAP short-key prefix, so comparing tables whose leading user columns are `DOUBLE`, `FLOAT`, `STRING`, or `JSON` succeeds without manual key configuration.

---

## Supported Model Kinds & Catalog Matrix

| SQLMesh Model Kind | Doris Internal OLAP Catalog (`internal` on Local Disk or S3/GCS Storage Vault) | External Iceberg Catalog (`table_format iceberg` on S3/GCS/HDFS) | External Hive Catalog (`table_format hive` on S3/GCS/HDFS) | External Read-Only Lakehouse Catalogs (`paimon`, `lance`, `hudi`, `deltalake`, `lakesoul`) |
| --- | :---: | :---: | :---: | :---: |
| Source / Upstream `SELECT` | Supported | Supported (incl. `FOR TIME/VERSION AS OF`, `@branch`, `@tag`) | Supported (Doris 2.1+) | Supported (Read-Only via corresponding External Catalog; Lance also supports `s3()` / `local()` TVFs) |
| `FULL` | Supported (`CREATE TABLE ... AS SELECT` / `DELETE` + `INSERT`) | Supported (`CREATE TABLE ... ENGINE=iceberg` / `DELETE` + `INSERT`; requires Doris 4.1.0+ & Iceberg v2 for existing-table `DELETE`) | Supported on unpartitioned Hive tables (`CREATE TABLE ... ENGINE=hive` / `INSERT OVERWRITE TABLE`, requires Doris 2.1.6+ for writes; partitioned Hive `FULL` refreshes are not supported until full-table replacement is available) | Paimon: Read-Only in Doris 2.1–4.1.5 (write support tracked in `apache/doris#65086`); Lance/Hudi/Delta/LakeSoul: Read-Only (Write Guarded) |
| `INCREMENTAL_BY_TIME_RANGE` | Supported (`DELETE WHERE time_col >= low AND time_col <= high` + `INSERT`) | Supported (`DELETE` + `INSERT`; requires Doris 4.1.0+ & Iceberg v2) | Not supported by Hive in Doris (No row-level `DELETE`) | Not supported in Doris 2.1–4.1.5 (No GA row-level `DELETE`) |
| `INCREMENTAL_BY_UNIQUE_KEY` | Supported (Auto-promotes `unique_key` to `UNIQUE KEY`, `DELETE ... USING` + `INSERT` or native `MERGE INTO` on Doris 4.1+ `UNIQUE KEY` tables when `when_matched` / `merge_filter` is specified) | Supported (Batched key `DELETE` + `INSERT` when `when_matched` and `merge_filter` are omitted; native `MERGE INTO` is not supported on Iceberg targets; requires Doris 4.1.0+ & Iceberg v2) | Not supported by Hive in Doris (No `DELETE` / `MERGE INTO`) | Not supported in Doris 2.1–4.1.5 (No GA `DELETE` / `MERGE INTO`) |
| `INCREMENTAL_BY_PARTITION` | Supported (Column & time-truncation range `DELETE` + `INSERT`) | Supported (Partition `DELETE` + `INSERT`; requires Doris 4.1.0+ & Iceberg v2) | Not supported by Hive in Doris (No `DELETE`) | Not supported in Doris 2.1–4.1.5 (No GA `DELETE`) |
| `SCD_TYPE_2_BY_TIME` / `SCD_TYPE_2_BY_COLUMN` | Supported | Supported (Requires Doris 4.1.0+ & Iceberg v2 for row-level `DELETE`) | Not supported by Hive in Doris (No `DELETE`) | Not supported in Doris 2.1–4.1.5 (No GA `DELETE`) |
| `VIEW` | Supported (`CREATE OR REPLACE VIEW`) | Requires `internal` catalog (`environment_catalog_mapping`) | Requires `internal` catalog (`environment_catalog_mapping`) | Requires `internal` catalog (`environment_catalog_mapping`) |
| `VIEW (materialized true)` | Supported (Asynchronous MTMVs; synchronous MVs are base-table rollup indexes created separately on the base table) | Not supported by Doris inside external catalogs | Not supported by Doris inside external catalogs | Not supported by Doris inside external catalogs |
| `SEED` / Python DataFrame Models | Supported (Automatic `VARCHAR(255)` OLAP key coercion) | Supported | Supported (Requires Doris 2.1.6+ for writes) | Paimon: Read-Only in Doris 2.1–4.1.5; Lance/Hudi/Delta/LakeSoul: Write Guarded |
| `CUSTOM ('doris_ddl_only')` | Supported (DDL + `ALTER TABLE` schema evolution only) | Supported | Supported (Requires Doris 2.1.6+ for writes) | Not supported (Read-Only) |

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
| `physical_properties (not_null_columns = ('c1', 'c2'))` | Defines specified OLAP or Iceberg columns as `NOT NULL` at `CREATE TABLE` time (materializing OLAP `CTAS` via schema DDL + `INSERT INTO`, and preserving `NOT NULL` across `ALTER TABLE ... MODIFY COLUMN` widenings) |

---

## Troubleshooting & Doris Gotchas

| Symptom / Doris Error | Cause | Resolution |
| --- | --- | --- |
| `errCode = 2, replication num 3 is larger than available backends 1` | Doris defaults to `replication_num = 3`, which fails on single-BE local/CI clusters. | Set `replication_num: 1` under `model_defaults.physical_properties` in `config.yaml` for local dev. |
| `PARTITION BY` omitted on OLAP table (logged as a warning) | Doris OLAP tables with `PARTITION BY (...) ()` and no partition creation strategy have zero partitions and reject all `INSERT`s. | Add `auto_partition = 'true'`, `'dynamic_partition.enable' = 'true'`, or explicit `partitions = '...'` to `physical_properties`, or set `partition_by_time_column false` in `INCREMENTAL_BY_TIME_RANGE` if you do not want OLAP partitioning. |
| `Doris only supports creating views in the 'internal' catalog` | External Iceberg catalogs do not support Doris `VIEW` or `MATERIALIZED VIEW` objects. | Set `virtual_environment_mode: dev_only` and `environment_catalog_mapping: {"^(?!prod$).*": "internal"}` in `config.yaml`. |
| `Key column cannot be of type STRING / DOUBLE / ARRAY` | Doris forbids `STRING`/`TEXT`, floating-point (`FLOAT`/`DOUBLE`), and complex types as OLAP key columns. | SQLMesh automatically reorders valid key columns first and coerces untyped `STRING`/`TEXT` key columns to `VARCHAR(255)` (for keys longer than 255 bytes, cast or declare an explicit `VARCHAR(n)` up to `VARCHAR(65533)`). For explicit keys on `DOUBLE`/`FLOAT`, use `DECIMAL` or an integer/date key column. |
