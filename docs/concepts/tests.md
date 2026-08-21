# Testing

Testing allows you to protect your project from regression by continuously verifying that the output of each model matches your expectations. Unlike [audits](audits.md), tests are executed either on demand (for example, as part of a CI/CD job or via [`sqlmesh test`](../reference/cli.md#test)) or when a new [plan](plans.md) is created.

By default, `sqlmesh plan` runs unit tests only for models included in the plan (added, modified, or restated). Plans with no such models skip unit tests. Use `--all-tests` to run the full suite, or `--skip-tests` to run none.

Similar to unit testing in software development, SQLMesh evaluates the model's logic against predefined inputs and then compares the output to expected outcomes provided as part of each test.

A comprehensive suite of tests can empower data practitioners to work with confidence, as it allows them to ensure models behave as expected after changes have been applied to them.

## Creating tests

A test suite is a [YAML file](https://learnxinyminutes.com/docs/yaml/) contained in the `tests/` folder of a SQLMesh project, whose name begins with `test` and ends with either `.yaml` or `.yml`. It can contain one or more uniquely named unit tests, each with a number of attributes that define its behavior.

At minimum, a unit test must specify the model being tested, the input values for its upstream models, and the expected outputs for the target model's query and/or its [Common Table Expressions](glossary.md#cte). Other optional attributes include a description, the gateway to use, and a mapping that assigns values to [macro variables](macros/macro_variables.md) referenced in the model.

Learn more about the supported attributes in the [unit test structure section](#unit-test-structure).

### Example

In this example, we'll use the `sqlmesh_example.full_model` model, which is provided as part of the `sqlmesh init` command and is defined as follows:

```sql linenums="1"
MODEL (
  name sqlmesh_example.full_model,
  kind FULL,
  cron '@daily',
  grain item_id,
  audits (assert_positive_order_ids),
);

SELECT
  item_id,
  COUNT(DISTINCT id) AS num_orders,
FROM
  sqlmesh_example.incremental_model
GROUP BY item_id
```

This model aggregates the number of orders per `item_id` from the upstream `sqlmesh_example.incremental_model`. One way to test it is shown below:

```yaml linenums="1"
test_example_full_model:
  model: sqlmesh_example.full_model
  inputs:
    sqlmesh_example.incremental_model:
      rows:
      - id: 1
        item_id: 1
      - id: 2
        item_id: 1
      - id: 3
        item_id: 2
  outputs:
    query:
      rows:
      - item_id: 1
        num_orders: 2
      - item_id: 2
        num_orders: 1
```

This test verifies that `sqlmesh_example.full_model` correctly counts the number of orders per `item_id`. It provides three rows as input to `sqlmesh_example.incremental_model` and expects two rows as output from the target model's query.

### Testing CTEs

Individual CTEs within the model's query can also be tested. To demonstrate this, let's slightly modify the query of `sqlmesh_example.full_model` to include a CTE named `filtered_orders_cte`:

```sql linenums="1"
WITH filtered_orders_cte AS (
  SELECT
    id,
    item_id
  FROM
    sqlmesh_example.incremental_model
  WHERE
    item_id = 1
)
SELECT
  item_id,
  COUNT(DISTINCT id) AS num_orders,
FROM
  filtered_orders_cte
GROUP BY item_id
```

The following test verifies the output of this CTE before aggregation takes place:

```yaml linenums="1" hl_lines="13-19"
test_example_full_model:
  model: sqlmesh_example.full_model
  inputs:
    sqlmesh_example.incremental_model:
        rows:
        - id: 1
          item_id: 1
        - id: 2
          item_id: 1
        - id: 3
          item_id: 2
  outputs:
    ctes:
      filtered_orders_cte:
        rows:
          - id: 1
            item_id: 1
          - id: 2
            item_id: 1
    query:
      rows:
      - item_id: 1
        num_orders: 2
```

## Supported data formats

SQLMesh currently supports the following ways to define input and output data in unit tests:

1. Listing YAML dictionaries where columns are mapped to their values for each row
2. Listing the rows as comma-separated values (CSV)
3. Executing a SQL query against the testing connection to generate the data

The previous examples demonstrate the first method, which is the default way to define data in unit tests. The following examples will cover the remaining methods.

### Defining data as CSV

This is how we could define the same test as in the first [example](#example), but with the input data formatted as CSV:

```yaml linenums="1"
test_example_full_model:
  model: sqlmesh_example.full_model
  inputs:
    sqlmesh_example.incremental_model:
      format: csv
      rows: |
        id,item_id
        1,1
        2,1
        3,2
  outputs:
    query:
      rows:
      - item_id: 1
        num_orders: 2
      - item_id: 2
        num_orders: 1
```

### Generating data using SQL queries

This is how we could define the same test as in the first [example](#example), but with the input data generated from a SQL query:

```yaml linenums="1"
test_example_full_model:
  model: sqlmesh_example.full_model
  inputs:
    sqlmesh_example.incremental_model:
      query: |
        SELECT 1 AS id, 1 AS item_id
        UNION ALL
        SELECT 2 AS id, 1 AS item_id
        UNION ALL
        SELECT 3 AS id, 2 AS item_id
  outputs:
    query:
      rows:
      - item_id: 1
        num_orders: 2
      - item_id: 2
        num_orders: 1
```

## Using files to populate data

SQLMesh supports loading data from external files. To achieve this, you can use the `path` attribute, which specifies the pathname of the data to be loaded:

```yaml linenums="1"
test_example_full_model:
  model: sqlmesh_example.full_model
  inputs:
    sqlmesh_example.incremental_model:
      format: csv
      path: filepath/test_data.csv
```

When `format` is omitted, the file will be loaded as a YAML document.

## Omitting columns

Defining the complete inputs and expected outputs for wide tables, i.e. tables with many columns, can become cumbersome. Therefore, if certain columns can be safely ignored they may be omitted from any row and their value will be treated as `NULL` for that row.

Additionally, it's possible to test only a subset of the output columns by setting `partial` to `true` for the outputs of interest:

```yaml linenums="1"
  outputs:
    query:
      partial: true
      rows:
        - <column_name>: <column_value>
          ...
```

This is useful when the missing columns can't be treated as `NULL`, but we still want to ignore them. In order to apply this setting to _all_ expected outputs, set it under the `outputs` key:

```yaml linenums="1"
  outputs:
    partial: true
    ...
```

## Freezing time

Some models may use SQL expressions that compute datetime values at a given point in time, such as `CURRENT_TIMESTAMP`. Since these expressions are non-deterministic, it's not enough to simply specify an expected output value in order to test them.

Setting the `execution_time` macro variable addresses this problem by mocking out the current time in the context of the test, thus making its value deterministic.

The following example demonstrates how `execution_time` can be used to test a column that is computed using `CURRENT_TIMESTAMP`. The model we're going to test is defined as:

```sql linenums="1"
MODEL (
  name colors,
  kind FULL
);

SELECT
  'Yellow' AS color,
  CURRENT_TIMESTAMP AS created_at
```

And the corresponding test is:

```yaml linenums="1"
test_colors:
  model: colors
  outputs:
    query:
      - color: "Yellow"
        created_at: "2023-01-01 12:05:03"
  vars:
    execution_time: "2023-01-01 12:05:03"
```

It's also possible to set a time zone for `execution_time`, by including it in the timestamp string.

If a time zone is provided, it is currently required that the test's _expected_ datetime values are timestamps without time zone, meaning that they need to be offset accordingly.

Here's how we would write the above test if we wanted to freeze the time to UTC+2:

```yaml linenums="1"
test_colors:
  model: colors
  outputs:
    query:
      - color: "Yellow"
        created_at: "2023-01-01 10:05:03"
  vars:
    execution_time: "2023-01-01 12:05:03+02:00"
```

## Parameterized model names

Testing models with parameterized names, such as `@{gold}.some_schema.some_table`, is possible using Jinja:

```yaml linenums="1"
test_parameterized_model:
  model: {{ var('gold') }}.some_schema.some_table
  ...
```

For example, assuming `gold` is a [config variable](../reference/configuration.md#variables) with value `gold_db`, the above test would be rendered as:

```yaml linenums="1"
test_parameterized_model:
  model: gold_db.some_schema.some_table
  ...
```

## Automatic test generation

Creating tests manually can be repetitive and error-prone, which is why SQLMesh also provides a way to automate this process using the [`create_test` command](../reference/cli.md#create_test).

This command can generate a complete test for a given model, as long as the tables of its upstream models exist in the project's data warehouse and are already populated with data.

### Example

In this example, we'll show how to generate a test for `sqlmesh_example.incremental_model`, which is another model provided as part of the `sqlmesh init` command and is defined as follows:

```sql linenums="1"
MODEL (
  name sqlmesh_example.incremental_model,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date
  ),
  start '2020-01-01',
  cron '@daily',
  grain (id, event_date)
);

SELECT
  id,
  item_id,
  event_date,
FROM
  sqlmesh_example.seed_model
WHERE
  event_date BETWEEN @start_date AND @end_date
```

Firstly, we need to specify the input data for the upstream model `sqlmesh_example.seed_model`. The `create_test` command starts by executing a user-supplied query against the project's data warehouse to fetch this data.

For instance, the following query will return three rows from the table corresponding to the model `sqlmesh_example.seed_model`:

```sql linenums="1"
SELECT * FROM sqlmesh_example.seed_model LIMIT 3
```

Next, notice that `sqlmesh_example.incremental_model` contains a filter which references the `@start_date` and `@end_date` [macro variables](macros/macro_variables.md).

To make the generated test deterministic and thus ensure that it will always succeed, we need to define these variables and modify the above query to constrain `event_date` accordingly.

If we set `@start_date` to `'2020-01-01'` and `@end_date` to `'2020-01-04'`, the above query needs to be changed to:

```sql linenums="1"
SELECT * FROM sqlmesh_example.seed_model WHERE event_date BETWEEN '2020-01-01' AND '2020-01-04' LIMIT 3
```

Finally, combining it with the proper macro variable definitions, we can compute the expected output for the model's query in order to generate the complete test.

This can be achieved using the following command:

```
$ sqlmesh create_test sqlmesh_example.incremental_model --query sqlmesh_example.seed_model "SELECT * FROM sqlmesh_example.seed_model WHERE event_date BETWEEN '2020-01-01' AND '2020-01-04' LIMIT 3" --var start '2020-01-01' --var end '2020-01-04'
```

Running this creates the following new test, located at `tests/test_incremental_model.yaml`:

```yaml linenums="1"
test_incremental_model:
  model: sqlmesh_example.incremental_model
  inputs:
    sqlmesh_example.seed_model:
    - id: 1
      item_id: 2
      event_date: 2020-01-01
    - id: 2
      item_id: 1
      event_date: 2020-01-01
    - id: 3
      item_id: 3
      event_date: 2020-01-03
  outputs:
    query:
    - id: 1
      item_id: 2
      event_date: 2020-01-01
    - id: 2
      item_id: 1
      event_date: 2020-01-01
    - id: 3
      item_id: 3
      event_date: 2020-01-03
  vars:
    start: '2020-01-01'
    end: '2020-01-04'
```

As you can see, we now have two passing tests. Hooray!

```
$ sqlmesh test
.
----------------------------------------------------------------------
Ran 2 tests in 0.024s

OK
```

## Using a different testing connection

The testing connection can be changed for a given test. This may be useful when, e.g., the model being tested cannot be correctly transpiled to the dialect of the default testing engine.

The following example demonstrates this by modifying `test_example_full_model`, so that it runs against a single-threaded local Spark process, defined as the `test_connection` of the `spark_testing` gateway in the project's `config.yaml` file:

```yaml linenums="1"
gateways:
  local:
    connection:
      type: duckdb
      database: db.db
  spark_testing:
    test_connection:
      type: spark
      config:
        # Run Spark locally with one worker thread
        "spark.master": "local"

        # Move data under /tmp so that it is only temporarily persisted
        "spark.sql.warehouse.dir": "/tmp/data_dir"
        "spark.driver.extraJavaOptions": "-Dderby.system.home=/tmp/derby_dir"

default_gateway: local

model_defaults:
  dialect: duckdb
```

The test would then be updated as follows:

```yaml linenums="1"
test_example_full_model:
  gateway: spark_testing
  # ... the other test attributes remain the same
```

## Running tests

Tests run automatically every time a new [plan](plans.md) is created, but they can also be executed on demand as described in the following sections.

### Testing using the CLI

You can execute tests on demand using the `sqlmesh test` command as follows:

```
$ sqlmesh test
.
----------------------------------------------------------------------
Ran 1 test in 0.005s

OK
```

The command returns a non-zero exit code if there are any failures, and reports them in the standard error stream:

```
$ sqlmesh test
F
======================================================================
FAIL: test_example_full_model (test/tests/test_full_model.yaml)
----------------------------------------------------------------------
AssertionError: Data mismatch (exp: expected, act: actual)

  num_orders
         exp  act
0        3.0  2.0

----------------------------------------------------------------------
Ran 1 test in 0.012s

FAILED (failures=1)
```

Note: when there are many differing columns, the corresponding dataframe will be truncated by default. In order to fully display them, use the `-v` (verbose) option of the `sqlmesh test` command.

To run a specific model test, pass in the suite file name followed by `::` and the name of the test:

```
$ sqlmesh test tests/test_full_model.yaml::test_example_full_model
```

You can also run tests that match a pattern or substring using a glob pathname expansion syntax:

```
$ sqlmesh test tests/test_*
```

### Testing using notebooks

You can execute tests on demand using the `%run_test` notebook magic as follows:

```
# This import will register all needed notebook magics
In [1]: import sqlmesh
        %run_test

        ----------------------------------------------------------------------
        Ran 1 test in 0.018s

        OK
```

The `%run_test` magic supports the same options as the corresponding [CLI command](#testing-using-the-CLI).

## Troubleshooting issues

When executing unit tests, SQLMesh creates input fixtures as views within the testing connection.

These fixtures are dropped by default after the execution completes, but it is possible to preserve them using the `--preserve-fixtures` option available in both the `sqlmesh test` CLI command and the `%run_test` notebook magic.

This can be helpful when debugging a test failure, because for example it's possible to query the fixture views directly and verify that they are defined correctly.

!!! note
    By default, the views that are necessary to run a unit test are created within a new, unique schema, whose name looks like `sqlmesh_test_<random_ID>`. To specify a custom name for this schema, set the [`<test_name>.schema`](#test_nameschema) test attribute.

### Type mismatches

It's not always possible to correctly interpret certain values in a unit test without additional context. For example, a YAML dictionary can be used to represent both a `STRUCT` and a `MAP` value in SQL.

To avoid this ambiguity, SQLMesh needs to know the columns' types. By default, it will try to infer these types based on the model definitions, but they can also be explicitly specified:

- in the [`external_models.yaml`](models/external_models.md#generating-an-external-models-schema-file) file (for external models)
- using the [`columns`](models/overview.md#columns) model property
- using the [`columns`](#creating_tests) attribute of the unit test

If none of these options work, consider using a SQL [query](#test_nameinputsupstream_modelquery) to generate the data.

## Unit test structure

### `<test_name>`

The unique name of the test.

### `<test_name>.model`

The name of the model being tested. This model must be defined in the project's `models/` folder.

### `<test_name>.description`

An optional description of the test, which can be used to provide additional context.

### `<test_name>.schema`

The name of the schema that will contain the views that are necessary to run this unit test.

### `<test_name>.gateway`

The gateway whose `test_connection` will be used to run this test. If not specified, the default gateway is used.

### `<test_name>.inputs`

The inputs that will be used to test the target model. If the model has no dependencies, this can be omitted.

### `<test_name>.inputs.<upstream_model>`

A model that the target model depends on.

### `<test_name>.inputs.<upstream_model>.rows`

The rows of the upstream model, defined as an array of dictionaries that map columns to their values:

```yaml linenums="1"
    <upstream_model>:
      rows:
        - <column_name>: <column_value>
        ...
```

If `rows` is the only key under `<upstream_model>`, then it can be omitted:

```yaml linenums="1"
    <upstream_model>:
      - <column_name>: <column_value>
      ...
```

When the input format is `csv`, the data can be specified inline under `rows` :

```yaml linenums="1"
    <upstream_model>:
      rows: |
        <column1_name>,<column2_name>
        <row1_value>,<row1_value>
        <row2_value>,<row2_value>
```

### `<test_name>.inputs.<upstream_model>.format`
  
The optional `format` key allows for control over how the input data is loaded.

```yaml linenums="1"
    <upstream_model>:
      format: csv
```

Currently, the following formats are supported: `yaml` (default), `csv`.

### `<test_name>.inputs.<upstream_model>.csv_settings`
  
When the`format` is CSV, you can control the behaviour of data loading under `csv_settings`:

```yaml linenums="1"
    <upstream_model>:
      format: csv
      csv_settings: 
        sep: "#"
        skip_blank_lines: true
      rows: |
        <column1_name>#<column2_name>
        <row1_value>#<row1_value>
        <row2_value>#<row2_value>
```

Learn more about the [supported CSV settings](https://pandas.pydata.org/docs/reference/api/pandas.read_csv.html).
  
### `<test_name>.inputs.<upstream_model>.path`

The optional `path` key specifies the pathname of the data to be loaded.
  
```yaml linenums="1"
    <upstream_model>:
      path: filepath/test_data.yaml
```

### `<test_name>.inputs.<upstream_model>.columns`

An optional dictionary that maps columns to their types:

```yaml linenums="1"
    <upstream_model>:
      columns:
        <column_name>: <column_type>
        ...
```

This can be used to help SQLMesh interpret the row values correctly in the context of SQL.

Any number of columns may be omitted from this mapping, in which case their types will be inferred on a best-effort basis. Explicitly casting the corresponding columns in the model's query will enable SQLMesh to infer their types more accurately.

### `<test_name>.inputs.<upstream_model>.query`

An optional SQL query that will be executed against the testing connection to generate the input rows:

```yaml linenums="1"
    <upstream_model>:
      query: <sql_query>
```

This provides more control over how the input data must be interpreted.

The `query` key can't be used together with the `rows` key.

### `<test_name>.outputs`

The target model's expected outputs.

Note: the columns in each row of an expected output must appear in the same relative order as they are selected in the corresponding query.

### `<test_name>.outputs.partial`

A boolean flag that indicates whether only a subset of the output columns will be tested. When set to `true`, only the columns referenced in the corresponding expected rows will be tested.

See also: [Omitting columns](#omitting-columns).

### `<test_name>.outputs.query`

The expected output of the target model's query. This is optional, as long as [`<test_name>.outputs.ctes`](#test_nameoutputsctes) is present.

### `<test_name>.outputs.query.partial`

Same as [`<test_name>.outputs.partial`](#test_nameoutputspartial), but applies only to the output of the target model's query.

### `<test_name>.outputs.query.rows`

The expected rows of the target model's query.

See also: [`<test_name>.inputs.<upstream_model>.rows`](#test_nameinputsupstream_modelrows).

### `<test_name>.outputs.query.query`

An optional SQL query that will be executed against the testing connection to generate the expected rows for the target model's query.

See also: [`<test_name>.inputs.<upstream_model>.query`](#test_nameinputsupstream_modelquery).

### `<test_name>.outputs.ctes`

The expected output per each individual top-level [Common Table Expression](glossary.md#cte) (CTE) defined in the target model's query. This is optional, as long as [`<test_name>.outputs.query`](#test_nameoutputsquery) is present.

### `<test_name>.outputs.ctes.<cte_name>`

The expected output of the CTE with name `<cte_name>`.

### `<test_name>.outputs.ctes.<cte_name>.partial`

Same as [`<test_name>.outputs.partial`](#test_nameoutputs_partial), but applies only to the output of the CTE with name `<cte_name>`.

### `<test_name>.outputs.ctes.<cte_name>.rows`

The expected rows of the CTE with name `<cte_name>`.

See also: [`<test_name>.inputs.<upstream_model>.rows`](#test_nameinputsupstream_modelrows).

### `<test_name>.outputs.ctes.<cte_name>.query`

An optional SQL query that will be executed against the testing connection to generate the expected rows for the CTE with name `<cte_name`.

See also: [`<test_name>.inputs.<upstream_model>.query`](#test_nameinputsupstream_modelquery).

### `<test_name>.vars`

An optional dictionary that assigns values to macro variables:

```
  vars:
    start: 2022-01-01
    end: 2022-01-01
    execution_time: 2022-01-01
    <macro_variable_name>: <macro_variable_value>
```

There are three special macro variables: `start`, `end`, and `execution_time`. If these are set, they will override the corresponding date macros of the target model. For example, `@execution_ds` will render to `2022-01-01` if `execution_time` is set to this value.

Additionally, SQL expressions like `CURRENT_DATE` and `CURRENT_TIMESTAMP` will produce the same datetime value as `execution_time`, when it is set.
