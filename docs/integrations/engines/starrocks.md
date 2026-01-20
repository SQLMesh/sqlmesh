# StarRocks

## Overview

[StarRocks](https://www.starrocks.io/) is a next-generation sub-second MPP OLAP database designed for real-time analytics. It provides high concurrency, low latency, and supports both batch and stream processing.

SQLMesh supports StarRocks through its MySQL-compatible protocol, providing StarRocks-specific optimizations for table models, indexing, partitioning, and more. The adapter leverages StarRocks's strengths for analytical workloads with sensible defaults and advanced configuration support.

## Prerequisites

* Install SQLMesh with the StarRocks extra:

```bash
pip install "sqlmesh[starrocks]"
```

* Initialize a SQLMesh project (if you haven't already):

```bash
sqlmesh init
```

* Configure a separate state backend:
  * StarRocks is currently **not supported** as a SQLMesh `state_connection`.
  * Use DuckDB (recommended) or another engine for SQLMesh state.

## Connection Configuration Example

```yaml linenums="1" hl_lines="2 4-8 13-15"
gateways:
  starrocks:
    connection:
      type: starrocks
      host: starrocks-fe  # Frontend (FE) node address
      port: 9030          # Query port (default: 9030)
      user: starrocks_user
      password: your_password
      database: your_database
      # Optional MySQL-compatible settings
      # charset: utf8mb4
      # connect_timeout: 60
    state_connection:
      type: duckdb
      database: ./state/sqlmesh_state.db

default_gateway: starrocks

model_defaults:
  dialect: starrocks
```

### StarRocks setup note (optional)

If you're running a shared-nothing cluster with a single backend, you may need to adjust the default replication number:

```sql
ADMIN SET frontend config ("default_replication_num" = "1");
```

## Quickstart

### 1) A minimal table (DUPLICATE KEY default)

```sql
MODEL (
  name user_events,
  kind FULL,
  physical_properties (
    distributed_by = RANDOM
  )
);

SELECT
  user_id,
  event_time,
  event_type
FROM source.user_events;
```

A `DUPLICATE KEY` table can usually be used as a `FULL` kind model.

### 2) An incremental table (PRIMARY KEY recommended)

```sql
MODEL (
  name user_events_inc,
  kind INCREMENTAL_BY_TIME_RANGE(
    time_column event_date
  ),
  physical_properties (
    primary_key = (user_id, event_date),
    partition_by = (date_trunc('day', event_date)),
    distributed_by = (kind=HASH, expressions=user_id, buckets=16)
  )
);

SELECT
  user_id,
  event_date,
  COUNT(*) AS cnt
FROM source.user_events
WHERE event_date BETWEEN @start_ds AND @end_ds
GROUP BY user_id, event_date;
```

## Table Types

StarRocks supports four table types: **DUPLICATE KEY**, **PRIMARY KEY**, **UNIQUE KEY**, and **AGGREGATE KEY**.

SQLMesh configures StarRocks table types via `physical_properties` (engine-specific table properties).

> **Note**: StarRocks `AGGREGATE KEY` requires per-value-column aggregation functions, which SQLMesh model syntax **DOES NOT** currently support. Use `PRIMARY KEY` or `DUPLICATE KEY` instead.

### DUPLICATE KEY Type (Default)

If you do not set a key type, StarRocks creates a DUPLICATE KEY table by default.

**Example:**

```sql
MODEL (
  name user_events,
  kind FULL,
  physical_properties (
    distributed_by = RANDOM
  )
);
```

### PRIMARY KEY Type

For incremental models, **PRIMARY KEY tables are needed** (and effectively required for robust deletes) because StarRocks supports *weaker* `DELETE ... WHERE ...` on non-primary-key table types.

SQLMesh will apply conservative `WHERE` transformations for compatibility (for example, converting `BETWEEN` to `>= AND <=`, removing boolean literals, and converting `DELETE ... WHERE TRUE` to `TRUNCATE TABLE`). To avoid limitations and keep incremental maintenance reliable, use a `PRIMARY KEY` table by setting `physical_properties.primary_key`.

> SQLMesh currently does not support specifying `primary_key` as a model parameter.

**Example (INCREMENTAL_BY_TIME_RANGE):**

```sql
MODEL (
  name user_events,
  kind INCREMENTAL_BY_TIME_RANGE(
    time_column event_date
  ),
  physical_properties (
    primary_key = (user_id, event_date),
    distributed_by = (kind=HASH, expressions=user_id, buckets=16)
  )
);

SELECT
  user_id,
  event_date,
  COUNT(*) AS cnt
FROM source.user_events
WHERE event_date BETWEEN @start_ds AND @end_ds
GROUP BY user_id, event_date;
```

### UNIQUE KEY Type

You can create a UNIQUE KEY table by setting `physical_properties.unique_key`. In most incremental use cases, a PRIMARY KEY table is recommended instead.

**Example:**

```sql
MODEL (
  name user_events_unique,
  kind FULL,
  physical_properties (
    unique_key = (user_id, event_date),
    distributed_by = (kind=HASH, expressions=user_id, buckets=16)
  )
);
```

## Table Properties

This section documents StarRocks engine-specific table properties via `physical_properties (...)` (table properties). Most properties support:

* **Structured form** (recommended): easier validation and clearer intent
* **String fallback**: for convenience or when you want to paste native StarRocks syntax quickly

Most of the time, the value syntax is the same or similar as a corresponding clause in StarRocks, espacially for a **string** type value.

When specifying **string** values, prefer **single quotes**.

### Configuration Matrix

| Property | Where | Recommended form | String fallback | Notes |
| --- | --- | --- | --- | --- |
| `primary_key` | `physical_properties` | `primary_key = (col1, col2)` | `primary_key = 'col1, col2'` | Required for PRIMARY KEY tables (recommended for incremental). |
| `duplicate_key` | `physical_properties` | `duplicate_key = (col1, col2)` | `duplicate_key = 'col1, col2'` | Explicitly sets DUPLICATE KEY table type. |
| `unique_key` | `physical_properties` | `unique_key = (col1, col2)` | `unique_key = 'col1, col2'` | Sets UNIQUE KEY table type. |
| `partitioned_by` / `partition_by` | `MODEL` / `physical_properties` | `partitioned_by (dt)` (model param) / `partition_by = RANGE(dt, region)` (table property) | `partition_by = 'RANGE(dt, region)'` | Its' recommended to use `partition_by` in `physical_properties` for RANGE/LIST partitioning together with `partitions`. |
| `partitions` | `physical_properties` | `partitions = ('PARTITION ...', 'PARTITION ...')` | `partitions = 'PARTITION ...'` | Initial partitions; easiest to express as strings. When using RANGE or LIST partitioning, you need to specify initial `partitions`. |
| `distributed_by` | `physical_properties` | `distributed_by = (kind=HASH, expressions=(c1, c2), buckets=10)` | `distributed_by = 'HASH(c1, c2) BUCKETS 10'` / `distributed_by = 'RANDOM'` | |
| `clustered_by` / `order_by` | `MODEL` / `physical_properties` | `clustered_by (col1, col2)` / `order_by = (col1, col2)` | `order_by = 'col1, col2'` | Ordering/clustering columns for query performance if it's not the same as the table key. |
| Other properties | `physical_properties` | Use strings (recommended) | Use strings | StarRocks `PROPERTIES` are string key/value pairs. |

**Notes:**

* You can use enum-like values without quotes (for example `HASH`, `RANDOM`, `IMMEDIATE`), but strings are also accepted (prefer single quotes).
* Aliases exist for convenience: use `partition_by` (table property) as an alias of `partitioned_by` (model parameter), and `order_by` ↔ `clustered_by`.
* Only several properties can be set as model
parameters: `partitioned_by`, `clustered_by`. But, for
simplity, you're recommended to use table properties
only.

### Table Key Properties

Table key properties accept multiple forms:

* **Structured**: `col` or `(col1, col2, ...)`
* **String**: `'col'` or `'col1, col2'`

**Syntax:**

* Structured: `primary_key = col`, `primary_key = (col1, col2)`, `duplicate_key = (col2)`
* String: `primary_key = 'col1, col2'`, `unique_key = '(col2, col3)'`.

#### PRIMARY KEY

```sql
MODEL (
  name my_pk_table,
  kind FULL,
  physical_properties (
    primary_key = (id, ds),
    distributed_by = (kind=HASH, expressions=id, buckets=10)
  )
);
```

#### DUPLICATE KEY

```sql
MODEL (
  name my_dup_table,
  kind FULL,
  physical_properties (
    duplicate_key = (id, ds),
    distributed_by = RANDOM
  )
);
```

#### UNIQUE KEY

```sql
MODEL (
  name my_unique_table,
  kind FULL,
  physical_properties (
    unique_key = (id, ds),
    distributed_by = (kind=HASH, expressions=id, buckets=10)
  )
);
```

### Partitioning

StarRocks supports `RANGE` partitioning, `LIST` partitioning, and **expression partitioning**.

You can specify partitioning either:

* As a **model parameter**: `partitioned_by (...)` (good for simple expressions)
* As a **table property**: `physical_properties(partition_by=...)` (recommended when you need RANGE/LIST, or complex expressions)

For `RANGE` and `LIST` partitioning, you generally need to provide initial `partitions` (pre-created partitions). For expression partitioning, `partitions` is usually not needed.

#### `partitioned_by` / `partition_by`

NOTE:

* `partitioned_by (...)` can only be used as a model parameter (SQLMesh enforces this constraint).
* `partition_by` can be provided in `physical_properties` as table properties (for advanced partitioning).

**Syntax:**

* Expression list: `partitioned_by (col)` / `partitioned_by (expr1, expr2)`
  * for complex example: `partition_by = (date_trunc('day', col2), col3)`
* RANGE/LIST: `partition_by = RANGE(col1, col2)` / `partition_by = LIST(col1, col2)`
* String fallback: `partition_by = 'RANGE(col1, col2)'`

#### `partitions`

**Syntax:**

* Tuple of strings: `partitions = ('PARTITION ...', 'PARTITION ...')`
* Single string: `partitions = 'PARTITION ...'`

#### Expression partitioning

```sql
MODEL (
  name my_partitioned_model,
  kind INCREMENTAL_BY_TIME_RANGE(time_column event_date),
  partitioned_by (date_trunc('day', event_time), region),
  physical_properties (
    primary_key = (user_id, event_date, region),
    distributed_by = (kind=HASH, expressions=user_id, buckets=10)
  )
);
```

#### RANGE partitioning

```sql
MODEL (
  name my_partitioned_model_advanced,
  kind FULL,
  physical_properties (
    partition_by = RANGE(event_time),
    partitions = (
      'PARTITION p20240101 VALUES [("2024-01-01"), ("2024-01-02"))',
      'PARTITION p20240102 VALUES [("2024-01-02"), ("2024-01-03"))'
    ),
    distributed_by = (kind=HASH, expressions=region, buckets=10)
  )
);
```

It's similar for `LIST` partitioning as `RANGE` partitioning.

### Distribution

StarRocks supports both `HASH` and `RANDOM` distribution. You can use a structured value or a string.

1. Structured type syntax: ```(kind=<kind> [, expressions=<columns>] [, buckets=<num>])```

   * **kind**: `HASH` OR `RANDOM`.
   * **expressions**: a single column or a tuple of columns, such as `col1` or `(col1, col2)`. (optional)
   * **buckets**: bucket number. (optional)

2. String type is similar as: `'HASH(id) BUCKETS 10'`, which is the same as the distribution clause in StarRocks's `CREATE TABLE`.
3. Or even a single enum-like value: `distributed_by = RANDOM`.

#### HASH distribution

Structured type (recommended):

```sql
MODEL (
  name my_table,
  kind FULL,
  physical_properties (
    distributed_by = (kind=HASH, expressions=(user_id), buckets=10)
  )
);
```

#### RANDOM distribution

Simple enumerate type:

```sql
MODEL (
  name my_table_random,
  kind FULL,
  physical_properties (
    distributed_by = RANDOM
  )
);
```

#### String fallback

A single string, which is the same as the clause in StarRocks's `CREATE TABLE`.

```sql
MODEL (
  name my_table_string_dist,
  kind FULL,
  physical_properties (
    distributed_by = 'HASH(user_id) BUCKETS 10'
  )
);
```

### Ordering

You can use `clustered_by` or `order_by` to specify the column ordering to optimize query performance if it's not the same the table key.

You can specify `clustered_by` both as a model parameter and a table property, but you can only specify `order_by` as a table property.

**Syntax:**

* Structured: `order_by = col` / `order_by = (col1, col2)`
* String fallback: `order_by = 'col1, col2'`

```sql
MODEL (
  name my_ordered_table,
  kind FULL,
  physical_properties (
    order_by = (ds, id),
    distributed_by = (kind=HASH, expressions=id, buckets=10)
  )
);
```

### Generic PROPERTIES

Any additional properties in `physical_properties` are passed through as StarRocks `PROPERTIES`. Since StarRocks `PROPERTIES` values are typically strings, using strings is recommended.

```sql
MODEL (
  name advanced_table,
  kind FULL,
  physical_properties (
    primary_key = (id),
    distributed_by = (kind=HASH, expressions=id, buckets=8),
    replication_num = '1',
    storage_medium = 'SSD',
    enable_persistent_index = 'true',
    compression = 'LZ4'
  )
);
```

## Views and Materialized Views

### Views

StarRocks supports view `SECURITY` via **`virtual_properties`**.`security`.

**Syntax:**

* `security = INVOKER` or `security = NONE`. (optional)

```sql
MODEL (
  name user_summary_view,
  kind VIEW,
  virtual_properties (
    security = INVOKER
  )
);

SELECT
  user_id,
  COUNT(*) AS event_count,
  MAX(event_time) AS last_event_time
FROM user_events
GROUP BY user_id;
```

### Materialized Views (MV)

SQLMesh uses `kind VIEW (materialized true)` to create materialized views.

For ASYNC MVs, StarRocks requires a `REFRESH` clause, so you must specify **at least one** of `refresh_moment` or `refresh_scheme`.

MV properties (including `refresh_moment` / `refresh_scheme` and other table-like properties such as partitioning, distribution, ordering, and generic properties) must be specified in **`physical_properties`**.

**Refresh properties:**

* `refresh_moment`: `IMMEDIATE` or `DEFERRED` (optional)
* `refresh_scheme`: `MANUAL` or `ASYNC ...` (optional)
  * If you specify it with the `START/EVERY`, you must specify it as a whole string, quoted by a pair of quotes.
  * Examples: `ASYNC`, `MANUAL`, `ASYNC START ("2024-01-01 00:00:00") EVERY (INTERVAL 5 MINUTE)`
  * The syntax of `ASYNC ...` clause is the same as the clause in StarRocks.

```sql
MODEL (
  name user_summary_mv,
  kind VIEW (
    materialized true
  ),
  physical_properties (
    refresh_moment = DEFERRED,
    refresh_scheme = 'ASYNC START ("2024-01-01 00:00:00") EVERY (INTERVAL 5 MINUTE)'
  )
);

SELECT
  user_id,
  COUNT(*) AS event_count,
  MAX(event_time) AS last_event_time
FROM user_events
GROUP BY user_id;
```

**Other properties:**

You can specify `partitioning`, `distribution`, `order by` and `properties` the same as normal table properties. But notice that only supported MV properties are useful, Refer to StarRocks' doc for MV creation.

**Notes:**

* If you create materialized views with `replace=true`, SQLMesh may drop and recreate the MV. When an MV is dropped, its data is removed and the MV must be refreshed/rebuilt again.
* There are some restriction for `partitioning`, you need to refer StarRocks' doc for MV partitioning specification.
* StarRocks MV schema supports a column list but does **not** support explicit data types in that list. Column data types come from the `AS SELECT ...` query.
* If you create MVs from a dataframe via the Python API, provide `target_columns_to_types` (a `Dict[str, exp.DataType]`). If you don't care about exact types, you can set all columns to `VARCHAR` as a fallback:

```python
from sqlglot import exp

target_columns_to_types = {
    "col1": exp.DataType.build("VARCHAR"),
    "col2": exp.DataType.build("VARCHAR"),
}
```

## Limitations

* **No SYNC MV support**: synchronous materialized views are not supported yet.
* **No tuple IN**: StarRocks does not support `(c1, c2) IN ((v1, v2), ...)`.
* **No `SELECT ... FOR UPDATE`**: StarRocks is an OLAP database and does not support row locks; SQLMesh removes `FOR UPDATE` when executing SQLGlot expressions.
* **RENAME caveat**: `ALTER TABLE db.old RENAME db.new` is not supported; the `RENAME` target cannot be qualified with a database name.

## Dependencies

To use StarRocks with SQLMesh, install the required MySQL driver:

```bash
pip install "sqlmesh[starrocks]"
# or
pip install pymysql
```

## Resources

* [StarRocks Documentation](https://docs.starrocks.io/)
* [StarRocks Table Design Guide](https://docs.starrocks.io/docs/table_design/StarRocks_table_design/)
* [StarRocks SQL Reference](https://docs.starrocks.io/docs/sql-reference/sql-statements/data-definition/CREATE_TABLE/)
