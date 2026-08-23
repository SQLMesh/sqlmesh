# ClickHouse

This page describes SQLMesh support for the ClickHouse engine, including configuration options specific to ClickHouse.

!!! note
    ClickHouse may not be used for the SQLMesh [state connection](../../reference/configuration.md#connections).

## Background

[ClickHouse](https://clickhouse.com/) is a distributed, column-oriented SQL engine designed to rapidly execute analytical workloads.

It provides users fine-grained control of its behavior, but that control comes at the cost of complex configuration.

This section provides background information about ClickHouse, providing context for how to use SQLMesh with the ClickHouse engine.

### Object naming

Most SQL engines use a three-level hierarchical naming scheme: tables/views are nested within _schemas_, and schemas are nested within _catalogs_. For example, the full name of a table might be `my_catalog.my_schema.my_table`.

ClickHouse instead uses a two-level hierarchical naming scheme that has no counterpart to _catalog_. In addition, it calls the second level in the hierarchy "databases." SQLMesh and its documentation refer to this second level as "schemas."

SQLMesh fully supports ClickHouse's two-level naming scheme without user action.

### Table engines

Every ClickHouse table is created with a ["table engine" that controls how the table's data is stored and queried](https://clickhouse.com/docs/en/engines/table-engines). ClickHouse's (and SQLMesh's) default table engine is `MergeTree`.

The [`MergeTree` engine family](https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/mergetree) requires that every table be created with an `ORDER BY` clause.

SQLMesh will automatically inject an empty `ORDER BY` clause into every `MergeTree` family table's `CREATE` statement, or you can specify the columns/expressions by which the table should be ordered.

### ClickHouse modes of operation

Conceptually, it may be helpful to view ClickHouse as having three modes of operation: single server, cluster, and ClickHouse Cloud. SQLMesh supports all three modes.

#### Single server mode

Single server mode is similar to other SQL engines: aside from choosing each table's engine, you do not need to worry about how computations are executed. You issue standard SQL commands/queries, and ClickHouse executes them.

#### Cluster mode

Cluster mode allows you to scale your ClickHouse engine to any number of networked servers. This enables massive workloads, but requires that you specify how computations are executed by the networked servers.

ClickHouse coordinates the computations on the networked servers with [ClickHouse Keeper](https://clickhouse.com/docs/en/architecture/horizontal-scaling) (it also supports [Apache ZooKeeper](https://zookeeper.apache.org/)).

You specify named virtual clusters of servers in the Keeper configuration, and those clusters provide namespaces for data objects and computations. For example, you might include all networked servers in the cluster you name `MyCluster`.

In general, you must be connected to a ClickHouse server to execute commands. By default, each command you execute runs in single-server mode on the server you are connected to.

To associate an object with a cluster, DDL commands that create or modify it must include the text `ON CLUSTER [your cluster name]`.

If you provide a cluster name in your SQLMesh connection configuration, SQLMesh will automatically inject the `ON CLUSTER` statement into the DDL commands for all objects created while executing the project. We provide more information about clusters in SQLMesh [below](#cluster-specification).

#### ClickHouse Cloud mode

[ClickHouse Cloud](https://clickhouse.com/cloud) is a managed ClickHouse platform. It allows you to scale ClickHouse without administering a cluster yourself or modifying your SQL commands to run on the cluster.

ClickHouse Cloud automates ClickHouse's cluster controls, which sometimes constrains ClickHouse's flexibility or how you execute SQL commands. For example, creating a table with a `SELECT` command must [occur in two steps on ClickHouse Cloud](https://clickhouse.com/docs/en/sql-reference/statements/create/table#from-select-query). SQLMesh handles this limitation for you.

Aside from those constraints, ClickHouse Cloud mode is similar to single server mode - you run standard SQL commands/queries, and ClickHouse Cloud executes them.

## Permissions

In the default SQLMesh configuration, users must have sufficient permissions to create new ClickHouse databases.

Alternatively, you can configure specific databases where SQLMesh should create table and view objects.

### Environment views

Use the [`environment_suffix_target` key in your project configuration](../../guides/configuration.md#disable-environment-specific-schemas) to specify that environment views should be created within the model's database instead of in a new database:

``` yaml
environment_suffix_target: table
```

### Physical tables

Use the [`physical_schema_mapping` key in your project configuration](../../guides/configuration.md#physical-table-schemas) to specify the databases where physical tables should be created.

The key accepts a dictionary of regular expressions that map model database names to the corresponding databases where physical tables should be created.

SQLMesh will compare a model's database name to each regular expression and use the first match to determine which database a physical table should be created in.

For example, this configuration places every model's physical table in the `model_physical_tables` database because the regular expression `.*` matches any database name:

``` yaml
physical_schema_mapping:
  '.*': model_physical_tables
```

## Cluster specification

A ClickHouse cluster allows multiple networked ClickHouse servers to operate on the same data object. Every cluster must be named in the ClickHouse configuration files, and that name is passed to a table's DDL statements in the `ON CLUSTER` clause.

For example, we could create a table `my_schema.my_table` on cluster `TheCluster` like this: `CREATE TABLE my_schema.my_table ON CLUSTER TheCluster (col1 Int8)`.

To create SQLMesh objects on a cluster, provide the cluster name to the `cluster` key in the SQLMesh connection definition (see all connection parameters [below](#localbuilt-in-scheduler)).

SQLMesh will automatically inject the `ON CLUSTER` clause and cluster name you provide into all project DDL statements.

## Model definition

This section describes how you control a table's engine and other ClickHouse-specific functionality in SQLMesh models.

### Table engine

SQLMesh uses the `MergeTree` table engine with an empty `ORDER BY` clause by default.

Specify a different table engine by passing the table engine definition to the model DDL's `storage_format` parameter. For example, you could specify the `Log` table engine like this:

``` sql linenums="1" hl_lines="4"
MODEL (
    name my_schema.my_log_table,
    kind full,
    storage_format Log,
);

select
    *
from other_schema.other_table;
```

You may also specify more complex table engine definitions. For example:

``` sql linenums="1" hl_lines="4"
MODEL (
    name my_schema.my_rep_table,
    kind full,
    storage_format ReplicatedMergeTree('/clickhouse/tables/{shard}/table_name', '{replica}', ver),
);

select
    *
from other_schema.other_table;
```

#### ORDER BY

`MergeTree` family engines require that a table's `CREATE` statement include the `ORDER BY` clause.

SQLMesh will automatically inject an empty `ORDER BY ()` when creating a table with an engine in the `MergeTree` family. This creates the table without any ordering.

You may specify columns/expressions to `ORDER BY` by passing them to the model `physical_properties` dictionary's `order_by` key.

For example, you could order by columns `col1` and `col2` like this:

``` sql linenums="1" hl_lines="4-6"
MODEL (
    name my_schema.my_log_table,
    kind full,
    physical_properties (
        order_by = (col1, col2)
    )
);

select
    *
from other_schema.other_table;
```

Note that there is an `=` between the `order_by` key name and value `(col1, col2)`.

Complex `ORDER BY` expressions may need to be passed in single quotes, with interior single quotes escaped by the `\` character.

#### PRIMARY KEY

Table engines may also accept a `PRIMARY KEY` specification. Similar to `ORDER BY`, specify a primary key in the model DDL's `physical_properties` dictionary. For example:

``` sql linenums="1" hl_lines="6"
MODEL (
    name my_schema.my_log_table,
    kind full,
    physical_properties (
        order_by = (col1, col2),
        primary_key = col1
    )
);

select
    *
from other_schema.other_table;
```

Note that there is an `=` between the `primary_key` key name and value `col1`.

### TTL

ClickHouse tables accept a [TTL expression that triggers actions](https://clickhouse.com/docs/en/guides/developer/ttl) like deleting rows after a certain amount of time has passed.

Similar to `ORDER_BY` and `PRIMARY_KEY`, specify a TTL key in the model DDL's `physical_properties` dictionary. For example:

``` sql linenums="1" hl_lines="6"
MODEL (
    name my_schema.my_log_table,
    kind full,
    physical_properties (
        order_by = (col1, col2),
        primary_key = col1,
        ttl = timestamp + INTERVAL 1 WEEK
    )
);

select
    *
from other_schema.other_table;
```

Note that there is an `=` between the `ttl` key name and value `timestamp + INTERVAL 1 WEEK`.

### Partitioning

Some ClickHouse table engines support partitioning. Specify the partitioning columns/expressions in the model DDL's `partitioned_by` key.

For example, you could partition by columns `col1` and `col2` like this:

``` sql linenums="1" hl_lines="4"
MODEL (
    name my_schema.my_log_table,
    kind full,
    partitioned_by (col1, col2),
);

select
    *
from other_schema.other_table;
```

Learn more below about how SQLMesh uses [partitioned tables to improve performance](#performance-considerations).

## Settings

ClickHouse supports an [immense number of settings](https://clickhouse.com/docs/en/operations/settings), many of which can be altered in multiple places: ClickHouse configuration files, Python client connection arguments, DDL statements, SQL queries, and others.

This section discusses how to control ClickHouse settings in SQLMesh.

### Connection settings

SQLMesh connects to Python with the [`clickhouse-connect` library](https://clickhouse.com/docs/en/integrations/python). Its connection method accepts a dictionary of arbitrary settings that are passed to ClickHouse.

Specify these settings in the `connection_settings` key. This example demonstrates how to set the `distributed_ddl_task_timeout` setting to `300`:

``` yaml linenums="1" hl_lines="8-9"
clickhouse_gateway:
  connection:
    type: clickhouse
    host: localhost
    port: 8123
    username: user
    password: pw
    connection_settings:
      distributed_ddl_task_timeout: 300
  state_connection:
    type: duckdb
```

### DDL settings

ClickHouse settings may also be specified in DDL commands like `CREATE`.

Specify these settings in a model DDL's [`physical_properties` key](https://sqlmesh.readthedocs.io/en/stable/concepts/models/overview/?h=physical#physical_properties) (where the [`order_by`](#order-by) and [`primary_key`](#primary-key) values are specified, if present).

This example demonstrates how to set the `index_granularity` setting to `128`:

``` sql linenums="1" hl_lines="4-6"
MODEL (
    name my_schema.my_log_table,
    kind full,
    physical_properties (
        index_granularity = 128
    )
);

select
    *
from other_schema.other_table;
```

Note that there is an `=` between the `index_granularity` key name and value `128`.

### Query settings

ClickHouse settings may be specified directly in a model's query with the `SETTINGS` keyword.

This example demonstrates setting the `join_use_nulls` setting to `1`:

``` sql linenums="1" hl_lines="9"
MODEL (
    name my_schema.my_log_table,
    kind full,
);

select
    *
from other_schema.other_table
SETTINGS join_use_nulls = 1;
```

Multiple settings may be specified in a query with repeated use of the `SETTINGS` keyword: `SELECT * FROM other_table SETTINGS first_setting = 1 SETTINGS second_setting = 2;`.

#### Usage by SQLMesh

The ClickHouse setting `join_use_nulls` affects the behavior of SQLMesh SCD models and table diffs. This section describes how SQLMesh uses query settings to control that behavior.

^^Background^^

In general, table `JOIN`s can return empty cells for rows not present in both tables.

For example, consider `LEFT JOIN`ing two tables `left` and `right`, where the column `right_column` is only present in the `right` table. Any rows only present in the `left` table will have no value for `right_column` in the joined table.

In other SQL engines, those empty cells are filled with `NULL`s.

In contrast, ClickHouse fills the empty cells with data type-specific default values (e.g., 0 for integer column types). It will instead fill the cells with `NULL`s if you set the `join_use_nulls` setting to `1`.

^^SQLMesh^^

SQLMesh automatically generates SQL queries for both SCD Type 2 models and table diff comparisons. These queries include table `JOIN`s and calculations based on the presence of `NULL` values.

Because those queries expect `NULL` values in empty cells, SQLMesh automatically adds `SETTINGS join_use_nulls = 1` to the generated SCD and table diff SQL code.

The SCD model definition query is embedded as a CTE in the full SQLMesh-generated query. If run alone, the model definition query would use the ClickHouse server's current `join_use_nulls` value.

If that value is not `1`, the SQLMesh setting on the outer query would override the server value and produce incorrect results.

Therefore, SQLMesh uses the following procedure to ensure the model definition query runs with the correct `join_use_nulls` value:

- If the model query sets `join_use_nulls` itself, do nothing
- If the model query does not set `join_use_nulls` and the current server `join_use_nulls` value is `1`, do nothing
- If the model query does not set `join_use_nulls` and the current server `join_use_nulls` value is `0`, add `SETTINGS join_use_nulls = 0` to the CTE model query
    - All other CTEs and the outer query will still execute with a `join_use_nulls` value of `1`

## Performance considerations

ClickHouse is optimized for writing/reading records, so deleting/replacing records can be extremely slow.

This section describes why SQLMesh needs to delete/replace records and how the ClickHouse engine adapter works around the limitations.

### Why delete or replace?

SQLMesh "materializes" model kinds in a number of ways, such as:

- Replacing an entire table ([`FULL` models](../../concepts/models/model_kinds.md#full))
- Replacing records in a specific time range ([`INCREMENTAL_BY_TIME_RANGE` models](../../concepts/models/model_kinds.md#incremental_by_time_range))
- Replacing records with specific key values ([`INCREMENTAL_BY_UNIQUE_KEY` models](../../concepts/models/model_kinds.md#incremental_by_unique_key))
- Replacing records in specific partitions ([`INCREMENTAL_BY_PARTITION` models](../../concepts/models/model_kinds.md#incremental_by_partition))

Different SQL engines provide different methods for performing record replacement.

Some engines natively support updating or inserting ("upserting") records. For example, in some engines you can `merge` a new table into an existing table based on a key. Records in the new table whose keys are already in the existing table will update/replace the existing records. Records in the new table without keys in the existing table will be inserted into the existing table.

Other engines do not natively support upserts, so SQLMesh replaces records in two steps: delete the records to update/replace from the existing table, then insert the new records.

ClickHouse does not support upserts, and it performs the two step delete/insert operation so slowly as to be unusable. Therefore, SQLMesh uses a different method for replacing records.

### Temp table swap

SQLMesh uses what we call the "temp table swap" method of replacing records in ClickHouse.

Because ClickHouse is optimized for writing and reading records, it is often faster to copy most of a table than to delete a small portion of its records. That is the approach used by the temp table swap method (with optional performance improvements [for partitioned tables](#partition-swap)).

The temp table swap has four steps:

1. Make an empty temp copy of the existing table that has the same structure (columns, data types, table engine, etc.)
2. Insert new records into the temp table
3. Insert the existing records that should be **kept** into the temp table
4. Swap the table names, such that the temp table now has the existing table's name

Figure 1 illustrates these four steps:
<br></br>

![ClickHouse table swap steps](./clickhouse/clickhouse_table-swap-steps.png){ loading=lazy }
_Figure 1: steps to execute a temp table swap_
<br></br>

The weakness of this method is that it requires copying all existing rows to keep (step three), which can be problematic for large tables.

To address this weakness, SQLMesh instead uses *partition* swapping if a table is partitioned.

### Partition swap

ClickHouse supports *partitioned* tables, which store groups of records in separate files, or "partitions."

A table is partitioned based on a table column or SQL expression - the "partitioning key." All records with the same value for the partitioning key are stored together in a partition.

For example, consider a table containing each record's creation date in a datetime column. If we partition the table by month, all the records whose timestamp was in January will be stored in one partition, records from February in another partition, and so on.

Table partitioning provides a major benefit for improving swap performance: records can be inserted, updated, or deleted in individual partitions.

SQLMesh leverages this to avoid copying large numbers of existing records into a temp table. Instead, it only copies the records that are in partitions affected by a load's newly ingested records.

SQLMesh automatically uses partition swapping for any incremental model that specifies the [`partitioned_by`](../../concepts/models/overview.md#partitioned_by) key.

#### Choosing a partitioning key

The first step of partitioning a table is choosing its partitioning key (columns or expression). The primary consideration for a key is the total number of partitions it will generate, which affects table performance.

Too many partitions can drastically decrease performance because the overhead of handling partition files swamps the benefits of copying fewer records. Too few partitions decreases swap performance because many existing records must still be copied in each incremental load.

!!! question "How many partitions is too many?"

    ClickHouse's documentation [specifically warns against tables having too many partitions](https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/custom-partitioning-key), suggesting a maximum of 1000.

The total number of partitions in a table is determined by the actual data in the table, not by the partition column/expression alone.

For example, consider a table partitioned by date. If we insert records created on `2024-10-23`, the table will have one partition. If we then insert records from `2024-10-24`, the table will have two partitions. One partition is created for each unique value of the key.

For each partitioned table in your project, carefully consider the number of partitions created by the combination of your partitioning expression and the characteristics of your data.

#### Incremental by time models

`INCREMENTAL_BY_TIME_RANGE` kind models must be partitioned by time. If the model's `time_column` is not present in any `partitioned_by` expression, SQLMesh will automatically add it as the first partitioning expression.

By default, `INCREMENTAL_BY_TIME_RANGE` models partition by week, so the maximum recommended 1000 partitions corresponds to about 19 years of data. SQLMesh projects have widely varying time ranges and data sizes, so you should choose a model's partitioning key based on the data your system will process.

If a model has many records in each partition, you may see additional performance benefits by including the time column in the model's [`ORDER_BY` expression](#order-by).

!!! info "Partitioning by time"
    `INCREMENTAL_BY_TIME_RANGE` models must be partitioned by time.

    SQLMesh will automatically partition them by **week** unless the `partitioned_by` configuration key includes the time column or an expression based on it.

    Choose a model's time partitioning granularity based on the characteristics of the data it will process, making sure the total number of partitions is 1000 or fewer.

## Multi-gateway setup

ClickHouse does not have a catalog concept — its fully-qualified table names are two-level (`database.table`), not three-level (`catalog.database.table`).

When a SQLMesh project uses ClickHouse alongside a catalog-aware gateway such as Trino or BigQuery, the two gateway types produce FQNs with different nesting depths. SQLMesh's internal schema tracking requires uniform nesting, so it assigns a **virtual catalog** to ClickHouse models at load time.

### How the virtual catalog works

- SQLMesh automatically detects the nesting mismatch and injects a virtual catalog into each ClickHouse adapter when a catalog-aware gateway is also present.
- ClickHouse models will appear with three-level FQNs in `sqlmesh plan` output and logs — for example, `__ch_prod__.mydb.mytable` for a gateway named `ch_prod`.
- The virtual catalog prefix is **never sent to ClickHouse**. It is stripped from every DDL and DML statement before execution.
- When ClickHouse is the only gateway in a project, no virtual catalog is assigned and models remain two-level.

### Adding a second gateway to an existing ClickHouse-only project

!!! warning "Re-materialization required"
    Adding a catalog-aware gateway (such as Trino or BigQuery) to a project that previously used ClickHouse as the only gateway triggers a **full re-materialization of every ClickHouse model** on the next `sqlmesh apply`. Plan for this before making the change.

If your project previously used ClickHouse as the only gateway, your models were fingerprinted with 2-level FQNs (`db.table`). Adding a catalog-aware gateway causes all ClickHouse models to be treated as new versions (their FQNs change to `__{gateway_name}__.db.table`):

- `FULL` models are recreated once — cost is proportional to the size of each table.
- `INCREMENTAL_BY_TIME_RANGE` models require a **full historical backfill** from the model's configured start date.
- The old 2-level model names appear as **Removed** in the plan and will be cleaned up after the environment TTL expires.

This is a one-time cost at the transition point and does not recur. There is no way to skip it — `--forward-only` does not apply because SQLMesh treats the 3-level names as new models, not modified ones.

### Virtual catalog naming

By default, the virtual catalog name is derived from **the gateway name you chose in your config**, wrapped in double underscores — for example, a gateway named `clickhouse` produces `__clickhouse__`, and a gateway named `ch_prod` produces `__ch_prod__`. The double-underscore wrapping makes it visually clear that this is an internal SQLMesh concept, not a real ClickHouse object.

You can override the default name by setting `virtual_catalog` in your ClickHouse connection configuration:

```yaml
gateways:
  clickhouse:
    connection:
      type: clickhouse
      host: my-clickhouse-host
      username: default
      virtual_catalog: ch_virtual  # optional; defaults to __{gateway_name}__ (e.g. __clickhouse__)
  trino:
    connection:
      type: trino
      ...
```

With this configuration, ClickHouse models will appear as `ch_virtual.mydb.mytable` in plan output instead of `__clickhouse__.mydb.mytable`.

## Local/Built-in Scheduler

**Engine Adapter Type**: `clickhouse`

| Option                    | Description                                                                                                                                                                                                                                                                     |  Type  | Required |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----: | :------: |
| `type`                    | Engine type name - must be `clickhouse`                                                                                                                                                                                                                                         | string |    Y     |
| `host`                    | ClickHouse server hostname or IP address                                                                                                                                                                                                                                        | string |    Y     |
| `username`                | ClickHouse user name                                                                                                                                                                                                                                                            | string |    Y     |
| `password`                | ClickHouse user password                                                                                                                                                                                                                                                        | string |    N     |
| `port`                    | The ClickHouse HTTP or HTTPS port (Default: `8123`)                                                                                                                                                                                                                             |  int   |    N     |
| `cluster`                 | ClickHouse cluster name                                                                                                                                                                                                                                                         | string |    N     |
| `connect_timeout`         | Connection timeout in seconds (Default: `10`)                                                                                                                                                                                                                                   |  int   |    N     |
| `send_receive_timeout`    | Send/receive timeout in seconds (Default: `300`)                                                                                                                                                                                                                                |  int   |    N     |
| `query_limit`             | Query result limit (Default: `0` - no limit)                                                                                                                                                                                                                                    |  int   |    N     |
| `use_compression`         | Whether to use compression (Default: `True`)                                                                                                                                                                                                                                    |  bool  |    N     |
| `compression_method`      | Compression method to use                                                                                                                                                                                                                                                       | string |    N     |
| `http_proxy`              | HTTP proxy address (equivalent to setting the HTTP_PROXY environment variable)                                                                                                                                                                                                  | string |    N     |
| `verify`                  | Verify server TLS/SSL certificate (Default: `True`)                                                                                                                                                                                                                             |  bool  |    N     |
| `ca_cert`                 | Ignored if verify is `False`. If verify is `True`, the file path to Certificate Authority root to validate ClickHouse server certificate, in .pem format. Not necessary if the ClickHouse server certificate is a globally trusted root as verified by the operating system.    | string |    N     |
| `client_cert`             | File path to a TLS Client certificate in .pem format (for mutual TLS authentication). The file should contain a full certificate chain, including any intermediate certificates.                                                                                                | string |    N     |
| `client_cert_key`         | File path to the private key for the Client Certificate. Required if the private key is not included the Client Certificate key file.                                                                                                                                           | string |    N     |
| `https_proxy`             | HTTPS proxy address (equivalent to setting the HTTPS_PROXY environment variable)                                                                                                                                                                                                | string |    N     |
| `server_host_name`        | The ClickHouse server hostname as identified by the CN or SNI of its TLS certificate. Set this to avoid SSL errors when connecting through a proxy or tunnel with a different hostname.                                                                                         | string |    N     |
| `tls_mode`                | Controls advanced TLS behavior. proxy and strict do not invoke ClickHouse mutual TLS connection, but do send client cert and key. mutual assumes ClickHouse mutual TLS auth with a client certificate.                                                                          | string |    N     |
| `connection_settings`     | Additional [connection settings](https://clickhouse.com/docs/integrations/python#settings-argument)                                                                                                                                                                             |  dict  |    N     |
| `connection_pool_options` | Additional [options](https://clickhouse.com/docs/integrations/python#customizing-the-http-connection-pool)                                                                                                                                         for the HTTP connection pool |  dict  |    N     |
| `virtual_catalog`         | Override the virtual catalog name used when ClickHouse runs alongside a catalog-aware gateway (e.g. Trino). Defaults to `__{gateway_name}__`. See [Multi-gateway setup](#multi-gateway-setup) for details.                                                                      | string |    N     |
