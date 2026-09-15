# Cross-database Table Diffing

Tobiko Cloud extends SQLMesh's [within-database table diff tool](../../guides/tablediff.md) to support comparison of tables or views across different database systems.

It provides a method of validating models that can be used along with [evaluating a model](../../guides/models.md#evaluating-a-model) and [testing a model with unit tests](../../guides/testing.md#testing-changes-to-models).

!!! tip "Learn more about table diffing"

    Learn more about using the table diff tool in the SQLMesh [table diff guide](../../guides/tablediff.md).

## Diffing tables or views across gateways

SQLMesh executes a project's models with a single database system, specified as a [gateway](../../guides/connections.md) in the project configuration.

The within-database table diff tool described above compares tables or environments within such a system. Sometimes, however, you might want to compare tables that reside in two different data systems.

For example, you might migrate your data transformations from an on-premises SQL engine to a cloud SQL engine while setting up your SQLMesh project. To demonstrate equivalence between the systems you could run the transformations in both and compare the new tables to the old tables.

The [within-database table diff](../../guides/tablediff.md) tool cannot make those comparisons, for two reasons:

1. It must join the two tables being diffed, but with two systems no single database engine can access both tables.
2. It assumes that data values can be compared across tables without modification. However, the diff must account for differences in data types across the two SQL engines (e.g., whether timestamps should include time zone information).

SQLMesh's cross-database table diff tool is built for just this scenario. Its comparison algorithm efficiently diffs tables without moving them from one system to the other and automatically addresses differences in data types.

## Configuration and syntax

To diff tables across systems, first configure a [gateway](../../reference/configuration.md#gateway) for each database system in your SQLMesh configuration file.

This example configures `bigquery` and `snowflake` gateways:

```yaml linenums="1"
gateways:
  bigquery:
    connection:
      type: bigquery
      [other connection parameters]

  snowflake:
    connection:
      type: snowflake
      [other connection parameters]
```

Then, specify each table's gateway in the `table_diff` command with this syntax: `[source_gateway]|[source table]:[target_gateway]|[target table]`.

For example, we could diff the `landing.table` table across `bigquery` and `snowflake` gateways like this:

```sh
tcloud sqlmesh table_diff 'bigquery|landing.table:snowflake|landing.table'
```

This syntax tells SQLMesh to use the cross-database diffing algorithm instead of the normal within-database diffing algorithm.

After adding gateways to the table names, use `table_diff` as described in the [SQLMesh table diff guide](../../guides/tablediff.md) - the same options apply for specifying the join keys, decimal precision, etc. See `tcloud sqlmesh table_diff --help` for a [full list of options](../../reference/cli.md#table_diff).

!!! warning

    Cross-database diff works for data objects (tables / views).

    Diffing _models_ is not supported because we do not assume that both the source and target databases are managed by SQLMesh.

## Example output

A cross-database diff is broken up into two stages.

The first stage is a schema diff. This example shows that differences in column name case across the two tables are identified as schema differences:

```bash
$ tcloud sqlmesh table_diff 'bigquery|sqlmesh_example.full_model:snowflake|sqlmesh_example.full_model' --on item_id --show-sample

Schema Diff Between 'BIGQUERY|SQLMESH_EXAMPLE.FULL_MODEL' and 'SNOWFLAKE|SQLMESH_EXAMPLE.FULL_MODEL':
├── Added Columns:
│   ├── ITEM_ID (DECIMAL(38, 0))
│   └── NUM_ORDERS (DECIMAL(38, 0))
└── Removed Columns:
    ├── item_id (BIGINT)
    └── num_orders (BIGINT)
Schema has differences; continue comparing rows? [y/n]:
```

SQLMesh prompts you before comparing data values across table rows. The prompt provides an opportunity to discontinue the comparison if the schemas are vastly different (potentially indicating a mistake) or you need to exclude columns from the diff because you know they won't match.

The second stage of the diff is comparing data values across tables. Within each system, SQLMesh divides the data into chunks, evaluates each chunk, and compares the outputs across systems. If a difference is found, it performs a row-level diff on that chunk by reading a sample of mismatched rows from each system.

This example shows that 2 rows were present in each system but had different values, one row was in Bigquery only, and one row was in Snowflake only:

```bash
Dividing source dataset into 10 chunks (based on 10947709 total records)
Checking chunks against target dataset
Chunk 1 hash mismatch!
Starting row-level comparison for the range (1 -> 3)
Identifying individual record hashes that don't match
Comparing

Row Counts:
├──  PARTIAL MATCH: 2 rows (66.67%)
├──  BIGQUERY ONLY: 1 rows (16.67%)
└──  SNOWFLAKE ONLY: 1 rows (16.67%)

COMMON ROWS column comparison stats:
            pct_match
num_orders        0.0


COMMON ROWS sample data differences:
Column: num_orders
┏━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┓
┃ item_id ┃ BIGQUERY ┃ SNOWFLAKE ┃
┡━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━┩
│ 1       │ 5        │ 7         │
│ 2       │ 1        │ 2         │
└─────────┴──────────┴───────────┘

BIGQUERY ONLY sample rows:
item_id num_orders
      7          4


SNOWFLAKE ONLY sample rows:
item_id num_orders
      4          6
```

If there are no differences found between chunks, the source and target datasets can be considered equal:

```bash
Chunk 1 (1094771 rows) matches!
Chunk 2 (1094771 rows) matches!
...
Chunk 10 (1094770 rows) matches!

All 10947709 records match between 'bigquery|sqlmesh_example.full_model' and 'snowflake|TEST.SQLMESH_EXAMPLE.FULL_MODEL'
```

!!! info

    Don't forget to specify the `--show-sample` option if you'd like to see a sample of the actual mismatched data!

    Otherwise, only high level statistics for the mismatched rows will be printed.

### Supported engines

Cross-database diffing is supported on all execution engines that [SQLMesh supports](../../integrations/overview.md#execution-engines).