# Model kinds

This page describes the kinds of [models](./overview.md) SQLMesh supports, which determine how the data for a model is loaded.

Find information about all model kind configuration parameters in the [model configuration reference page](../../reference/model_configuration.md).

## INCREMENTAL_BY_TIME_RANGE

Models of the `INCREMENTAL_BY_TIME_RANGE` kind are computed incrementally based on a time range. This is an optimal choice for datasets in which records are captured over time and represent immutable facts such as events, logs, or transactions. Using this kind for appropriate datasets typically results in significant cost and time savings.

Only missing time intervals are processed during each execution for `INCREMENTAL_BY_TIME_RANGE` models. This is in contrast to the [FULL](#full) model kind, where the entire dataset is recomputed every time the model is executed.

An `INCREMENTAL_BY_TIME_RANGE` model has two requirements that other models do not: it must know which column contains the time data it will use to filter the data by time range, and it must contain a `WHERE` clause that filters the upstream data by time.

The name of the column containing time data is specified in the model's `MODEL` DDL. It is specified in the DDL `kind` specification's `time_column` key. This example shows the `MODEL` DDL for an `INCREMENTAL_BY_TIME_RANGE` model that stores time data in the "event_date" column:

```sql linenums="1"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date -- This model's time information is stored in the `event_date` column
  )
);
```

<a id="timezones"></a>
In addition to specifying a time column in the `MODEL` DDL, the model's query must contain a `WHERE` clause that filters the upstream records by time range. SQLMesh provides special macros that represent the start and end of the time range being processed: `@start_date` / `@end_date` and `@start_ds` / `@end_ds`. Refer to [Macros](../macros/macro_variables.md) for more information.

??? "Example SQL sequence when applying this model kind (ex: BigQuery)"
    This is borrowed from the full walkthrough: [Incremental by Time Range](../../examples/incremental_time_full_walkthrough.md)

    Create a model with the following definition and run `sqlmesh plan dev`:

    ```sql
    MODEL (
      name demo.incrementals_demo,
      kind INCREMENTAL_BY_TIME_RANGE (
        -- How does this model kind behave?
        --   DELETE by time range, then INSERT
        time_column transaction_date,

        -- How do I handle late-arriving data?
        --   Handle late-arriving events for the past 2 (2*1) days based on cron
        --   interval. Each time it runs, it will process today, yesterday, and
        --   the day before yesterday.
        lookback 2,
      ),

      -- Don't backfill data before this date
      start '2024-10-25',

      -- What schedule should I run these at?
      --   Daily at Midnight UTC
      cron '@daily',

      -- Good documentation for the primary key
      grain transaction_id,

      -- How do I test this data?
      --   Validate that the `transaction_id` primary key values are both unique
      --   and non-null. Data audit tests only run for the processed intervals,
      --   not for the entire table.
      -- audits (
      --   UNIQUE_VALUES(columns = (transaction_id)),
      --   NOT_NULL(columns = (transaction_id))
      -- )
    );

    WITH sales_data AS (
      SELECT
        transaction_id,
        product_id,
        customer_id,
        transaction_amount,
        -- How do I account for UTC vs. PST (California baby) timestamps?
        --   Make sure all time columns are in UTC and convert them to PST in the
        --   presentation layer downstream.
        transaction_timestamp,
        payment_method,
        currency
      FROM sqlmesh-public-demo.tcloud_raw_data.sales  -- Source A: sales data
      -- How do I make this run fast and only process the necessary intervals?
      --   Use our date macros that will automatically run the necessary intervals.
      --   Because SQLMesh manages state, it will know what needs to run each time
      --   you invoke `sqlmesh run`.
      WHERE transaction_timestamp BETWEEN @start_dt AND @end_dt
    ),

    product_usage AS (
      SELECT
        product_id,
        customer_id,
        last_usage_date,
        usage_count,
        feature_utilization_score,
        user_segment
      FROM sqlmesh-public-demo.tcloud_raw_data.product_usage  -- Source B
      -- Include usage data from the 30 days before the interval
      WHERE last_usage_date BETWEEN DATE_SUB(@start_dt, INTERVAL 30 DAY) AND @end_dt
    )

    SELECT
      s.transaction_id,
      s.product_id,
      s.customer_id,
      s.transaction_amount,
      -- Extract the date from the timestamp to partition by day
      DATE(s.transaction_timestamp) as transaction_date,
      -- Convert timestamp to PST using a SQL function in the presentation layer for end users
      DATETIME(s.transaction_timestamp, 'America/Los_Angeles') as transaction_timestamp_pst,
      s.payment_method,
      s.currency,
      -- Product usage metrics
      p.last_usage_date,
      p.usage_count,
      p.feature_utilization_score,
      p.user_segment,
      -- Derived metrics
      CASE
        WHEN p.usage_count > 100 AND p.feature_utilization_score > 0.8 THEN 'Power User'
        WHEN p.usage_count > 50 THEN 'Regular User'
        WHEN p.usage_count IS NULL THEN 'New User'
        ELSE 'Light User'
      END as user_type,
      -- Time since last usage
      DATE_DIFF(s.transaction_timestamp, p.last_usage_date, DAY) as days_since_last_usage
    FROM sales_data s
    LEFT JOIN product_usage p
      ON s.product_id = p.product_id
      AND s.customer_id = p.customer_id
    ```

    SQLMesh will execute this SQL to create a versioned table in the physical layer. Note that the table's version fingerprint, `50975949`, is part of the table name.

    ```sql
    CREATE TABLE IF NOT EXISTS `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__incrementals_demo__50975949` (
      `transaction_id` STRING,
      `product_id` STRING,
      `customer_id` STRING,
      `transaction_amount` NUMERIC,
      `transaction_date` DATE OPTIONS (description='We extract the date from the timestamp to partition by day'),
      `transaction_timestamp_pst` DATETIME OPTIONS (description='Convert this to PST using a SQL function'),
      `payment_method` STRING,
      `currency` STRING,
      `last_usage_date` TIMESTAMP,
      `usage_count` INT64,
      `feature_utilization_score` FLOAT64,
      `user_segment` STRING,
      `user_type` STRING OPTIONS (description='Derived metrics'),
      `days_since_last_usage` INT64 OPTIONS (description='Time since last usage')
      )
      PARTITION BY `transaction_date`
    ```

    SQLMesh will validate the SQL before processing data (note the `WHERE FALSE LIMIT 0` and the placeholder timestamps).

    ```sql
    WITH `sales_data` AS (
      SELECT
        `sales`.`transaction_id` AS `transaction_id`,
        `sales`.`product_id` AS `product_id`,
        `sales`.`customer_id` AS `customer_id`,
        `sales`.`transaction_amount` AS `transaction_amount`,
        `sales`.`transaction_timestamp` AS `transaction_timestamp`,
        `sales`.`payment_method` AS `payment_method`,
        `sales`.`currency` AS `currency`
      FROM `sqlmesh-public-demo`.`tcloud_raw_data`.`sales` AS `sales`
      WHERE (
        `sales`.`transaction_timestamp` <= CAST('1970-01-01 23:59:59.999999+00:00' AS TIMESTAMP) AND
        `sales`.`transaction_timestamp` >= CAST('1970-01-01 00:00:00+00:00' AS TIMESTAMP)) AND
        FALSE
    ),
    `product_usage` AS (
      SELECT
        `product_usage`.`product_id` AS `product_id`,
        `product_usage`.`customer_id` AS `customer_id`,
        `product_usage`.`last_usage_date` AS `last_usage_date`,
        `product_usage`.`usage_count` AS `usage_count`,
        `product_usage`.`feature_utilization_score` AS `feature_utilization_score`,
        `product_usage`.`user_segment` AS `user_segment`
      FROM `sqlmesh-public-demo`.`tcloud_raw_data`.`product_usage` AS `product_usage`
      WHERE (
        `product_usage`.`last_usage_date` <= CAST('1970-01-01 23:59:59.999999+00:00' AS TIMESTAMP) AND
        `product_usage`.`last_usage_date` >= CAST('1969-12-02 00:00:00+00:00' AS TIMESTAMP)
        ) AND
        FALSE
    )

    SELECT
      `s`.`transaction_id` AS `transaction_id`,
      `s`.`product_id` AS `product_id`,
      `s`.`customer_id` AS `customer_id`,
      CAST(`s`.`transaction_amount` AS NUMERIC) AS `transaction_amount`,
      DATE(`s`.`transaction_timestamp`) AS `transaction_date`,
      DATETIME(`s`.`transaction_timestamp`, 'America/Los_Angeles') AS `transaction_timestamp_pst`,
      `s`.`payment_method` AS `payment_method`,
      `s`.`currency` AS `currency`,
      `p`.`last_usage_date` AS `last_usage_date`,
      `p`.`usage_count` AS `usage_count`,
      `p`.`feature_utilization_score` AS `feature_utilization_score`,
      `p`.`user_segment` AS `user_segment`,
      CASE
        WHEN `p`.`feature_utilization_score` > 0.8 AND `p`.`usage_count` > 100 THEN 'Power User'
        WHEN `p`.`usage_count` > 50 THEN 'Regular User'
        WHEN `p`.`usage_count` IS NULL THEN 'New User'
        ELSE 'Light User'
      END AS `user_type`,
      DATE_DIFF(`s`.`transaction_timestamp`, `p`.`last_usage_date`, DAY) AS `days_since_last_usage`
    FROM `sales_data` AS `s`
    LEFT JOIN `product_usage` AS `p`
      ON `p`.`customer_id` = `s`.`customer_id` AND
      `p`.`product_id` = `s`.`product_id`
    WHERE FALSE
    LIMIT 0
    ```

    SQLMesh will merge data into the empty table.

    ```sql
    MERGE INTO `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__incrementals_demo__50975949` AS `__MERGE_TARGET__` USING (
      WITH `sales_data` AS (
        SELECT
          `transaction_id`,
          `product_id`,
          `customer_id`,
          `transaction_amount`,
          `transaction_timestamp`,
          `payment_method`,
          `currency`
        FROM `sqlmesh-public-demo`.`tcloud_raw_data`.`sales` AS `sales`
        WHERE `transaction_timestamp` BETWEEN CAST('2024-10-25 00:00:00+00:00' AS TIMESTAMP) AND CAST('2024-11-04 23:59:59.999999+00:00' AS TIMESTAMP)
      ),
      `product_usage` AS (
        SELECT
          `product_id`,
          `customer_id`,
          `last_usage_date`,
          `usage_count`,
          `feature_utilization_score`,
          `user_segment`
        FROM `sqlmesh-public-demo`.`tcloud_raw_data`.`product_usage` AS `product_usage`
        WHERE `last_usage_date` BETWEEN DATE_SUB(CAST('2024-10-25 00:00:00+00:00' AS TIMESTAMP), INTERVAL '30' DAY) AND CAST('2024-11-04 23:59:59.999999+00:00' AS TIMESTAMP)
      )

      SELECT
        `transaction_id`,
        `product_id`,
        `customer_id`,
        `transaction_amount`,
        `transaction_date`,
        `transaction_timestamp_pst`,
        `payment_method`,
        `currency`,
        `last_usage_date`,
        `usage_count`,
        `feature_utilization_score`,
        `user_segment`,
        `user_type`,
        `days_since_last_usage`
      FROM (
        SELECT
          `s`.`transaction_id` AS `transaction_id`,
          `s`.`product_id` AS `product_id`,
          `s`.`customer_id` AS `customer_id`,
          `s`.`transaction_amount` AS `transaction_amount`,
          DATE(`s`.`transaction_timestamp`) AS `transaction_date`,
          DATETIME(`s`.`transaction_timestamp`, 'America/Los_Angeles') AS `transaction_timestamp_pst`,
          `s`.`payment_method` AS `payment_method`,
          `s`.`currency` AS `currency`,
          `p`.`last_usage_date` AS `last_usage_date`,
          `p`.`usage_count` AS `usage_count`,
          `p`.`feature_utilization_score` AS `feature_utilization_score`,
          `p`.`user_segment` AS `user_segment`,
          CASE
            WHEN `p`.`usage_count` > 100 AND `p`.`feature_utilization_score` > 0.8 THEN 'Power User'
            WHEN `p`.`usage_count` > 50 THEN 'Regular User'
            WHEN `p`.`usage_count` IS NULL THEN 'New User'
            ELSE 'Light User'
          END AS `user_type`,
          DATE_DIFF(`s`.`transaction_timestamp`, `p`.`last_usage_date`, DAY) AS `days_since_last_usage`
        FROM `sales_data` AS `s`
        LEFT JOIN `product_usage` AS `p`
          ON `s`.`product_id` = `p`.`product_id`
          AND `s`.`customer_id` = `p`.`customer_id`
      ) AS `_subquery`
      WHERE `transaction_date` BETWEEN CAST('2024-10-25' AS DATE) AND CAST('2024-11-04' AS DATE)
    ) AS `__MERGE_SOURCE__`
    ON FALSE
    WHEN NOT MATCHED BY SOURCE AND `transaction_date` BETWEEN CAST('2024-10-25' AS DATE) AND CAST('2024-11-04' AS DATE) THEN DELETE
    WHEN NOT MATCHED THEN
      INSERT (
        `transaction_id`, `product_id`, `customer_id`, `transaction_amount`, `transaction_date`, `transaction_timestamp_pst`,
        `payment_method`, `currency`, `last_usage_date`, `usage_count`, `feature_utilization_score`, `user_segment`, `user_type`,
        `days_since_last_usage`
      )
      VALUES (
        `transaction_id`, `product_id`, `customer_id`, `transaction_amount`, `transaction_date`, `transaction_timestamp_pst`,
        `payment_method`, `currency`, `last_usage_date`, `usage_count`, `feature_utilization_score`, `user_segment`, `user_type`,
        `days_since_last_usage`
      )
    ```

    SQLMesh will create a suffixed `__dev` schema based on the name of the plan environment.

    ```sql
    CREATE SCHEMA IF NOT EXISTS `sqlmesh-public-demo`.`demo__dev`
    ```

    SQLMesh will create a view in the virtual layer to pointing to the versioned table in the physical layer.

    ```sql
    CREATE OR REPLACE VIEW `sqlmesh-public-demo`.`demo__dev`.`incrementals_demo` AS
    SELECT *
    FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__incrementals_demo__50975949`
    ```

!!! tip "Important"

    A model's `time_column` should be in the [UTC time zone](https://en.wikipedia.org/wiki/Coordinated_Universal_Time) to ensure correct interaction with SQLMesh's scheduler and predefined macro variables.

    This requirement aligns with the data engineering best practice of converting datetime/timestamp columns to UTC as soon as they are ingested into the data system and only converting them to local timezones when they exit the system for downstream uses. The `cron_tz` flag **does not** change this requirement.

    Placing all timezone conversion code in the system's first/last transformation models prevents inadvertent timezone-related errors as data flows between models.

    If a model must use a different timezone, parameters like [lookback](./overview.md#lookback), [allow_partials](./overview.md#allow_partials), and [cron](./overview.md#cron) with offset time can be used to try to account for misalignment between the model's timezone and the UTC timezone used by SQLMesh.


This example implements a complete `INCREMENTAL_BY_TIME_RANGE` model that specifies the time column name `event_date` in the `MODEL` DDL and includes a SQL `WHERE` clause to filter records by time range:

```sql linenums="1" hl_lines="3-5 12-13"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date
  )
);

SELECT
  event_date::TEXT as event_date,
  event_payload::TEXT as payload
FROM raw_events
WHERE
  event_date BETWEEN @start_ds AND @end_ds;
```

### Time column
SQLMesh needs to know which column in the model's output represents the timestamp or date associated with each record.

!!! tip "Important"

    The `time_column` variable should be in the UTC time zone - learn more [above](#timezones).

The time column is used to determine which records will be overwritten during data [restatement](../plans.md#restatement-plans) and provides a partition key for engines that support partitioning (such as Apache Spark). The name of the time column is specified in the `MODEL` DDL `kind` specification:

```sql linenums="1" hl_lines="4"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date -- This model's time information is stored in the `event_date` column
  )
);
```

By default, SQLMesh assumes the time column is in the `%Y-%m-%d` format. For other formats, the default can be overridden with a formatting string:
```sql linenums="1" hl_lines="4"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column (event_date, '%Y-%m-%d')
  )
);
```

!!! note

    The time format should be defined using the same SQL dialect as the one used to define the model's query.

SQLMesh also uses the time column to automatically append a time range filter to the model's query at runtime, which prevents records that are not part of the target interval from being stored. This is a safety mechanism that prevents unintentionally overwriting unrelated records when handling late-arriving data.

The required filter you write in the model query's `WHERE` clause filters the **input** data as it is read from upstream tables, reducing the amount of data processed by the model. The automatically appended time range filter is applied to the model query's **output** data to prevent data leakage.

Consider the following model definition, which specifies a `WHERE` clause filter with the `receipt_date` column. The model's `time_column` is a different column `event_date`, whose filter is automatically added to the model query. This approach is useful when an upstream model's time column is different from the model's time column:

```sql linenums="1"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date -- `event_date` is model's time column
  )
);

SELECT
  event_date::TEXT as event_date,
  event_payload::TEXT as payload
FROM raw_events
WHERE
  receipt_date BETWEEN @start_ds AND @end_ds; -- Filter is based on the user-supplied `receipt_date` column
```

At runtime, SQLMesh will automatically modify the model's query to look like this:
```sql linenums="1" hl_lines="7"
SELECT
  event_date::TEXT as event_date,
  event_payload::TEXT as payload
FROM raw_events
WHERE
  receipt_date BETWEEN @start_ds AND @end_ds
  AND event_date BETWEEN @start_ds AND @end_ds; -- `event_date` time column filter automatically added by SQLMesh
```

### Partitioning

By default, we ensure that the `time_column` is part of the [partitioned_by](./overview.md#partitioned_by) property of the model so that it forms part of the partition key and allows the database engine to do partition pruning. If it is not explicitly listed in the Model definition, we will automatically add it.

However, this may be undesirable if you want to exclusively partition on another column or you want to partition on something like `month(time_column)` but the engine you're using doesnt support partitioning based on expressions.

To opt out of this behaviour, you can set `partition_by_time_column false` like so:

```sql linenums="1" hl_lines="5"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date,
    partition_by_time_column false
  ),
  partitioned_by (other_col) -- event_date will no longer be automatically added here and the partition key will just be 'other_col'
);
```

### Idempotency
We recommend making sure incremental by time range model queries are [idempotent](../glossary.md#idempotency) to prevent unexpected results during data [restatement](../plans.md#restatement-plans).

Note, however, that upstream models and tables can impact a model's idempotency. For example, referencing an upstream model of kind [FULL](#full) in the model query automatically causes the model to be non-idempotent because its data could change on every model execution.

### Materialization strategy
Depending on the target engine, models of the `INCREMENTAL_BY_TIME_RANGE` kind are materialized using the following strategies:

| Engine     | Strategy                                  |
|------------|-------------------------------------------|
| Spark      | INSERT OVERWRITE by time column partition |
| Databricks | INSERT OVERWRITE by time column partition |
| Snowflake  | DELETE by time range, then INSERT         |
| BigQuery   | DELETE by time range, then INSERT         |
| Redshift   | DELETE by time range, then INSERT         |
| Postgres   | DELETE by time range, then INSERT         |
| DuckDB     | DELETE by time range, then INSERT         |

## INCREMENTAL_BY_UNIQUE_KEY

Models of the `INCREMENTAL_BY_UNIQUE_KEY` kind are computed incrementally based on a key.

They insert or update rows based on these rules:

- If a key in newly loaded data is not present in the model table, the new data row is inserted.
- If a key in newly loaded data is already present in the model table, the existing row is updated with the new data.
- If a key is present in the model table but not present in the newly loaded data, its row is not modified and remains in the model table.

!!! important "Prevent duplicated keys"

    If you do not want duplicated keys in the model table, you must ensure the model query does not return rows with duplicate keys.

    SQLMesh does not automatically detect or prevent duplicates.

This kind is a good fit for datasets that have the following traits:

* Each record has a unique key associated with it.
* There is at most one record associated with each unique key.
* It is appropriate to upsert records, so existing records can be overwritten by new arrivals when their keys match.

A [Slowly Changing Dimension](../glossary.md#slowly-changing-dimension-scd) (SCD) is one approach that fits this description well. See the [SCD Type 2](#scd-type-2) model kind for a specific model kind for SCD Type 2 models.

The name of the unique key column must be provided as part of the `MODEL` DDL, as in this example:
```sql linenums="1" hl_lines="3-5"
MODEL (
  name db.employees,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key name
  )
);

SELECT
  name::TEXT as name,
  title::TEXT as title,
  salary::INT as salary
FROM raw_employees;
```

Composite keys are also supported:
```sql linenums="1" hl_lines="4"
MODEL (
  name db.employees,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key (first_name, last_name)
  )
);
```

`INCREMENTAL_BY_UNIQUE_KEY` model kinds can also filter upstream records by time range using a SQL `WHERE` clause and the `@start_date`, `@end_date` or other macro variables (similar to the [INCREMENTAL_BY_TIME_RANGE](#incremental_by_time_range) kind). Note that SQLMesh macro time variables are in the UTC time zone.
```sql linenums="1" hl_lines="6-7"
SELECT
  name::TEXT as name,
  title::TEXT as title,
  salary::INT as salary
FROM raw_employee_events
WHERE
  event_date BETWEEN @start_date AND @end_date;
```

??? "Example SQL sequence when applying this model kind (ex: BigQuery)"

    Create a model with the following definition and run `sqlmesh plan dev`:

    ```sql
    MODEL (
      name demo.incremental_by_unique_key_example,
      kind INCREMENTAL_BY_UNIQUE_KEY (
        unique_key id
      ),
      start '2020-01-01',
      cron '@daily',
    );

    SELECT
      id,
      item_id,
      event_date
    FROM demo.seed_model
    WHERE
      event_date BETWEEN @start_date AND @end_date
    ```

    SQLMesh will execute this SQL to create a versioned table in the physical layer. Note that the table's version fingerprint, `1161945221`, is part of the table name.

    ```sql
    CREATE TABLE IF NOT EXISTS `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__incremental_by_unique_key_example__1161945221` (`id` INT64, `item_id` INT64, `event_date` DATE)
    ```

    SQLMesh will validate the model's query before processing data (note the `FALSE LIMIT 0` in the `WHERE` statement and the placeholder dates).

    ```sql
    SELECT `seed_model`.`id` AS `id`, `seed_model`.`item_id` AS `item_id`, `seed_model`.`event_date` AS `event_date`
    FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__seed_model__2834544882` AS `seed_model`
    WHERE (`seed_model`.`event_date` <= CAST('1970-01-01' AS DATE) AND `seed_model`.`event_date` >= CAST('1970-01-01' AS DATE)) AND FALSE LIMIT 0
    ```

    SQLMesh will create a versioned table in the physical layer.

    ```sql
    CREATE OR REPLACE TABLE `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__incremental_by_unique_key_example__1161945221` AS
    SELECT CAST(`id` AS INT64) AS `id`, CAST(`item_id` AS INT64) AS `item_id`, CAST(`event_date` AS DATE) AS `event_date`
    FROM (SELECT `seed_model`.`id` AS `id`, `seed_model`.`item_id` AS `item_id`, `seed_model`.`event_date` AS `event_date`
    FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__seed_model__2834544882` AS `seed_model`
    WHERE `seed_model`.`event_date` <= CAST('2024-10-30' AS DATE) AND `seed_model`.`event_date` >= CAST('2020-01-01' AS DATE)) AS `_subquery`
    ```

    SQLMesh will create a suffixed `__dev` schema based on the name of the plan environment.

    ```sql
    CREATE SCHEMA IF NOT EXISTS `sqlmesh-public-demo`.`demo__dev`
    ```

    SQLMesh will create a view in the virtual layer pointing to the versioned table in the physical layer.

    ```sql
    CREATE OR REPLACE VIEW `sqlmesh-public-demo`.`demo__dev`.`incremental_by_unique_key_example` AS
    SELECT * FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__incremental_by_unique_key_example__1161945221`
    ```

**Note:** Models of the `INCREMENTAL_BY_UNIQUE_KEY` kind are inherently [non-idempotent](../glossary.md#idempotency), which should be taken into consideration during data [restatement](../plans.md#restatement-plans). As a result, partial data restatement is not supported for this model kind, which means that the entire table will be recreated from scratch if restated.

### Unique Key Expressions

The `unique_key` values can either be column names or SQL expressions. For example, if you wanted to create a key that is based on the coalesce of a value then you could do the following:

```sql linenums="1" hl_lines="4"
MODEL (
  name db.employees,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key COALESCE("name", '')
  )
);
```

### When Matched Expression

The logic to use when updating columns when a match occurs (the source and target match on the given keys) by default updates all the columns. This can be overridden with custom logic like below:

```sql linenums="1" hl_lines="5"
MODEL (
  name db.employees,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key name,
    when_matched (
      WHEN MATCHED THEN UPDATE SET target.salary = COALESCE(source.salary, target.salary)
    )
  )
);
```

The `source` and `target` aliases are required when using the `when_matched` expression in order to distinguish between the source and target columns.

Multiple `WHEN MATCHED` expressions can also be provided. Ex:

```sql linenums="1" hl_lines="5-6"
MODEL (
  name db.employees,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key name,
    when_matched (
      WHEN MATCHED AND source.value IS NULL THEN UPDATE SET target.salary = COALESCE(source.salary, target.salary)
      WHEN MATCHED THEN UPDATE SET target.title = COALESCE(source.title, target.title)
    )
  )
);
```

**Note**: `when_matched` is only available on engines that support the `MERGE` statement. Currently supported engines include:

* BigQuery
* Databricks
* Postgres
* Redshift
* Snowflake
* Spark

In Redshift's case, to enable the use of the native `MERGE` statement, you need to pass the `enable_merge` flag in the connection and set it to `true`. It is disabled by default.

```yaml linenums="1"
gateways:
  redshift:
    connection:
      type: redshift
      enable_merge: true
```

Redshift supports only the `UPDATE` or `DELETE` actions for the `WHEN MATCHED` clause and does not allow multiple `WHEN MATCHED` expressions. For further information, refer to the [Redshift documentation](https://docs.aws.amazon.com/redshift/latest/dg/r_MERGE.html#r_MERGE-parameters).

### Merge Filter Expression

The `MERGE` statement typically induces a full table scan of the existing table, which can be problematic with large data volumes.

Prevent a full table scan by passing filtering conditions to the `merge_filter` parameter.

The `merge_filter` accepts a single or a conjunction of predicates to be used in the `ON` clause of the `MERGE` operation:

```sql linenums="1" hl_lines="5"
MODEL (
  name db.employee_contracts,
  kind INCREMENTAL_BY_UNIQUE_KEY (
    unique_key id,
    merge_filter source._operation IS NULL AND target.contract_date > dateadd(day, -7, current_date)
  )
);
```

Similar to `when_matched`, the `source` and `target` aliases are used to distinguish between the source and target tables.

If an existing dbt project uses the [incremental_predicates](https://docs.getdbt.com/docs/build/incremental-strategy#about-incremental_predicates) functionality, SQLMesh will automatically convert them into the equivalent `merge_filter` specification.

### Materialization strategy
Depending on the target engine, models of the `INCREMENTAL_BY_UNIQUE_KEY` kind are materialized using the following strategies:

| Engine     | Strategy                            |
|------------|-------------------------------------|
| Spark      | not supported                       |
| Databricks | MERGE ON unique key                 |
| Snowflake  | MERGE ON unique key                 |
| BigQuery   | MERGE ON unique key                 |
| Redshift   | MERGE ON unique key                 |
| Postgres   | MERGE ON unique key                 |
| DuckDB     | DELETE ON matched + INSERT new rows |

## FULL
Models of the `FULL` kind cause the dataset associated with a model to be fully refreshed (rewritten) upon each model evaluation.

The `FULL` model kind is somewhat easier to use than incremental kinds due to the lack of special settings or additional query considerations. This makes it suitable for smaller datasets, where recomputing data from scratch is relatively cheap and doesn't require preservation of processing history. However, using this kind with datasets containing a large volume of records will result in significant runtime and compute costs.

This kind can be a good fit for aggregate tables that lack a temporal dimension. For aggregate tables with a temporal dimension, consider the [INCREMENTAL_BY_TIME_RANGE](#incremental_by_time_range) kind instead.

This example specifies a `FULL` model kind:
```sql linenums="1" hl_lines="3"
MODEL (
  name db.salary_by_title_agg,
  kind FULL
);

SELECT
  title,
  AVG(salary)
FROM db.employees
GROUP BY title;
```

??? "Example SQL sequence when applying this model kind (ex: BigQuery)"

    Create a model with the following definition and run `sqlmesh plan dev`:

    ```sql
    MODEL (
      name demo.full_model_example,
      kind FULL,
      cron '@daily',
      grain item_id,
    );

    SELECT
      item_id,
      COUNT(DISTINCT id) AS num_orders
    FROM demo.incremental_model
    GROUP BY
      item_id
    ```

    SQLMesh will execute this SQL to create a versioned table in the physical layer. Note that the table's version fingerprint, `2345651858`, is part of the table name.

    ```sql
    CREATE TABLE IF NOT EXISTS `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__full_model_example__2345651858` (`item_id` INT64, `num_orders` INT64)
    ```

    SQLMesh will validate the model's query before processing data (note the `WHERE FALSE` and `LIMIT 0`).

    ```sql
    SELECT `incremental_model`.`item_id` AS `item_id`, COUNT(DISTINCT `incremental_model`.`id`) AS `num_orders`
    FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__incremental_model__89556012` AS `incremental_model`
    WHERE FALSE
    GROUP BY `incremental_model`.`item_id` LIMIT 0
    ```

    SQLMesh will create a versioned table in the physical layer.

    ```sql
    CREATE OR REPLACE TABLE `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__full_model_example__2345651858` AS
    SELECT CAST(`item_id` AS INT64) AS `item_id`, CAST(`num_orders` AS INT64) AS `num_orders`
    FROM (SELECT `incremental_model`.`item_id` AS `item_id`, COUNT(DISTINCT `incremental_model`.`id`) AS `num_orders`
    FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__incremental_model__89556012` AS `incremental_model`
    GROUP BY `incremental_model`.`item_id`) AS `_subquery`
    ```

    SQLMesh will create a suffixed `__dev` schema based on the name of the plan environment.

    ```sql
    CREATE SCHEMA IF NOT EXISTS `sqlmesh-public-demo`.`demo__dev`
    ```

    SQLMesh will create a view in the virtual layer pointing to the versioned table in the physical layer.

    ```sql
    CREATE OR REPLACE VIEW `sqlmesh-public-demo`.`demo__dev`.`full_model_example` AS
    SELECT * FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__full_model_example__2345651858`
    ```

### Materialization strategy
Depending on the target engine, models of the `FULL` kind are materialized using the following strategies:

| Engine     | Strategy                         |
|------------|----------------------------------|
| Spark      | INSERT OVERWRITE                 |
| Databricks | INSERT OVERWRITE                 |
| Snowflake  | CREATE OR REPLACE TABLE          |
| BigQuery   | CREATE OR REPLACE TABLE          |
| Redshift   | DROP TABLE, CREATE TABLE, INSERT |
| Postgres   | DROP TABLE, CREATE TABLE, INSERT |
| DuckDB     | CREATE OR REPLACE TABLE          |

## VIEW
The model kinds described so far cause the output of a model query to be materialized and stored in a physical table.

The `VIEW` kind is different, because no data is actually written during model execution. Instead, a non-materialized view (or "virtual table") is created or replaced based on the model's query.

**Note:** `VIEW` is the default model kind if kind is not specified.

**Note:** Python models do not support the `VIEW` model kind - use a SQL model instead.

**Note:** With this kind, the model's query is evaluated every time the model is referenced in a downstream query. This may incur undesirable compute cost and time in cases where the model's query is compute-intensive, or when the model is referenced in many downstream queries.


This example specifies a `VIEW` model kind:
```sql linenums="1" hl_lines="3"
MODEL (
  name db.highest_salary,
  kind VIEW
);

SELECT
  MAX(salary)
FROM db.employees;
```

??? "Example SQL sequence when applying this model kind (ex: BigQuery)"

    Create a model with the following definition and run `sqlmesh plan dev`:

    ```sql
    MODEL (
      name demo.example_view,
      kind VIEW,
      cron '@daily',
    );

    SELECT
      'hello there' as a_column
    ```

    SQLMesh will execute this SQL to create a versioned view in the physical layer. Note that the view's version fingerprint, `1024042926`, is part of the view name.

    ```sql
    CREATE OR REPLACE VIEW `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__example_view__1024042926`
    (`a_column`) AS SELECT 'hello there' AS `a_column`
    ```

    SQLMesh will create a suffixed `__dev` schema based on the name of the plan environment.

    ```sql
    CREATE SCHEMA IF NOT EXISTS `sqlmesh-public-demo`.`demo__dev`
    ```

    SQLMesh will create a view in the virtual layer pointing to the versioned view in the physical layer.

    ```sql
    CREATE OR REPLACE VIEW `sqlmesh-public-demo`.`demo__dev`.`example_view` AS
    SELECT * FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__example_view__1024042926`
    ```


### Materialized Views
The `VIEW` model kind can be configured to represent a materialized view by setting the `materialized` flag to `true`:
```sql linenums="1" hl_lines="4"
MODEL (
  name db.highest_salary,
  kind VIEW (
    materialized true
  )
);
```

**Note:** This flag only applies to engines that support materialized views and is ignored by other engines. Supported engines include:

* BigQuery
* Databricks
* Snowflake

During the evaluation of a model of this kind, the view will be replaced or recreated only if the model's query rendered during evaluation does not match the query used during the previous view creation for this model, or if the target view does not exist. Thus, views are recreated only when necessary in order to realize all the benefits provided by materialized views.

## EMBEDDED
Embedded models are a way to share common logic between different models of other kinds.

There are no data assets (tables or views) associated with `EMBEDDED` models in the data warehouse. Instead, an `EMBEDDED` model's query is injected directly into the query of each downstream model that references it, as a subquery.

**Note:** Python models do not support the `EMBEDDED` model kind - use a SQL model instead.

This example specifies a `EMBEDDED` model kind:
```sql linenums="1" hl_lines="3"
MODEL (
  name db.unique_employees,
  kind EMBEDDED
);

SELECT DISTINCT
  name
FROM db.employees;
```

## SEED
The `SEED` model kind is used to specify [seed models](./seed_models.md) for using static CSV datasets in your SQLMesh project.

**Notes:**

- Seed models are loaded only once unless the SQL model and/or seed file is updated.
- Python models do not support the `SEED` model kind - use a SQL model instead.

??? "Example SQL sequence when applying this model kind (ex: BigQuery)"

    Create a model with the following definition and run `sqlmesh plan dev`:

    ```sql
    MODEL (
      name demo.seed_example,
      kind SEED (
        path '../../seeds/seed_example.csv'
      ),
      columns (
        id INT64,
        item_id INT64,
        event_date DATE
      ),
      grain (id, event_date)
    )
    ```

    SQLMesh will execute this SQL to create a versioned table in the physical layer. Note that the table's version fingerprint, `3038173937`, is part of the table name.

    ```sql
    CREATE TABLE IF NOT EXISTS `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__seed_example__3038173937` (`id` INT64, `item_id` INT64, `event_date` DATE)
    ```

    SQLMesh will upload the seed as a temp table in the physical layer.

    ```sql
    sqlmesh-public-demo.sqlmesh__demo.__temp_demo__seed_example__3038173937_9kzbpld7
    ```

    SQLMesh will create a versioned table in the physical layer from the temp table.

    ```sql
    CREATE OR REPLACE TABLE `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__seed_example__3038173937` AS
    SELECT CAST(`id` AS INT64) AS `id`, CAST(`item_id` AS INT64) AS `item_id`, CAST(`event_date` AS DATE) AS `event_date`
    FROM (SELECT `id`, `item_id`, `event_date`
    FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`__temp_demo__seed_example__3038173937_9kzbpld7`) AS `_subquery`
    ```

    SQLMesh will drop the temp table in the physical layer.

    ```sql
    DROP TABLE IF EXISTS `sqlmesh-public-demo`.`sqlmesh__demo`.`__temp_demo__seed_example__3038173937_9kzbpld7`
    ```

    SQLMesh will create a suffixed `__dev` schema based on the name of the plan environment.

    ```sql
    CREATE SCHEMA IF NOT EXISTS `sqlmesh-public-demo`.`demo__dev`
    ```

    SQLMesh will create a view in the virtual layer pointing to the versioned table in the physical layer.

    ```sql
    CREATE OR REPLACE VIEW `sqlmesh-public-demo`.`demo__dev`.`seed_example` AS
    SELECT * FROM `sqlmesh-public-demo`.`sqlmesh__demo`.`demo__seed_example__3038173937`
    ```

## SCD Type 2

SCD Type 2 is a model kind that supports [slowly changing dimensions](https://en.wikipedia.org/wiki/Slowly_changing_dimension#Type_2:_add_new_row) (SCDs) in your SQLMesh project. SCDs are a common pattern in data warehousing that allow you to track changes to records over time.

SQLMesh achieves this by adding a `valid_from` and `valid_to` column to your model. The `valid_from` column is the timestamp that the record became valid (inclusive) and the `valid_to` column is the timestamp that the record became invalid (exclusive). The `valid_to` column is set to `NULL` for the latest record.

Therefore, you can use these models to not only tell you what the latest value is for a given record but also what the values were anytime in the past. Note that maintaining this history does come at a cost of increased storage and compute and this may not be a good fit for sources that change frequently since the history could get very large.

**Note**: Partial data [restatement](../plans.md#restatement-plans) is not supported for this model kind, which means that the entire table will be recreated from scratch if restated. This may lead to data loss, so data restatement is disabled for models of this kind by default.

There are two ways to tracking changes: By Time (Recommended) or By Column.

### SCD Type 2 By Time (Recommended)

SCD Type 2 By Time supports sourcing from tables that have an "Updated At" timestamp defined in the table that tells you when a given record was last updated.
This is the recommended way since this "Updated At" gives you a precise time when the record was last updated and therefore improves the accuracy of the SCD Type 2 table that is produced.

This example specifies a `SCD_TYPE_2_BY_TIME` model kind:
```sql linenums="1" hl_lines="3"
MODEL (
  name db.menu_items,
  kind SCD_TYPE_2_BY_TIME (
    unique_key id,
  )
);

SELECT
  id::INT,
  name::STRING,
  price::DOUBLE,
  updated_at::TIMESTAMP
FROM
  stg.current_menu_items;
```

SQLMesh will materialize this table with the following structure:
```sql linenums="1"
TABLE db.menu_items (
  id INT,
  name STRING,
  price DOUBLE,
  updated_at TIMESTAMP,
  valid_from TIMESTAMP,
  valid_to TIMESTAMP
);
```

The `updated_at` column name can also be changed by adding the following to your model definition:
```sql linenums="1" hl_lines="5"
MODEL (
  name db.menu_items,
  kind SCD_TYPE_2_BY_TIME (
    unique_key id,
    updated_at_name my_updated_at -- Name for `updated_at` column
  )
);

SELECT
  id,
  name,
  price,
  my_updated_at
FROM
  stg.current_menu_items;
```

SQLMesh will materialize this table with the following structure:
```sql linenums="1"
TABLE db.menu_items (
  id INT,
  name STRING,
  price DOUBLE,
  my_updated_at TIMESTAMP,
  valid_from TIMESTAMP,
  valid_to TIMESTAMP
);
```

### SCD Type 2 By Column

SCD Type 2 By Column supports sourcing from tables that do not have an "Updated At" timestamp defined in the table.
Instead, it will check the columns defined in the `columns` field to see if their value has changed and if so it will record the `valid_from` time as the execution time when the change was detected.

This example specifies a `SCD_TYPE_2_BY_COLUMN` model kind:
```sql linenums="1" hl_lines="3"
MODEL (
  name db.menu_items,
  kind SCD_TYPE_2_BY_COLUMN (
    unique_key id,
    columns [name, price]
  )
);

SELECT
  id::INT,
  name::STRING,
  price::DOUBLE,
FROM
  stg.current_menu_items;
```

SQLMesh will materialize this table with the following structure:
```sql linenums="1"
TABLE db.menu_items (
  id INT,
  name STRING,
  price DOUBLE,
  valid_from TIMESTAMP,
  valid_to TIMESTAMP
);
```

### Change Column Names
SQLMesh will automatically add the `valid_from` and `valid_to` columns to your table.
If you would like to specify the names of these columns you can do so by adding the following to your model definition:
```sql linenums="1" hl_lines="5-6"
MODEL (
  name db.menu_items,
  kind SCD_TYPE_2_BY_TIME (
    unique_key id,
    valid_from_name my_valid_from, -- Name for `valid_from` column
    valid_to_name my_valid_to -- Name for `valid_to` column
  )
);
```

SQLMesh will materialize this table with the following structure:
```sql linenums="1"
TABLE db.menu_items (
  id INT,
  name STRING,
  price DOUBLE,
  updated_at TIMESTAMP,
  my_valid_from TIMESTAMP,
  my_valid_to TIMESTAMP
);
```

### Deletes

A hard delete is when a record no longer exists in the source table. When this happens,

If `invalidate_hard_deletes` is set to `false` (default):

* `valid_to` column will continue to be set to `NULL` (therefore still considered "valid")
* If the record is added back, then the `valid_to` column will be set to the `valid_from` of the new record.

When a record is added back, the new record will be inserted into the table with `valid_from` set to:

* SCD_TYPE_2_BY_TIME: the largest of either the `updated_at` timestamp of the new record or the `valid_from` timestamp of the deleted record in the SCD Type 2 table
* SCD_TYPE_2_BY_COLUMN: the `execution_time` when the record was detected again

If `invalidate_hard_deletes` is set to `true`:

* `valid_to` column will be set to the time when the SQLMesh run started that detected the missing record (called `execution_time`).
* If the record is added back, then the `valid_to` column will remain unchanged.

One way to think about `invalidate_hard_deletes` is that, if `invalidate_hard_deletes` is set to `true`, deletes are most accurately tracked in the SCD Type 2 table since it records when the delete occurred.
As a result though, you can have gaps between records if the there is a gap of time between when it was deleted and added back.
If you would prefer to not have gaps, and a result consider missing records in source as still "valid", then you can leave the default value or set `invalidate_hard_deletes` to `false`.

### Example of SCD Type 2 By Time in Action

Lets say that you started with the following data in your source table and `invalidate_hard_deletes` is set to `true`:

| ID | Name             | Price |     Updated At      |
|----|------------------|:-----:|:-------------------:|
| 1  | Chicken Sandwich | 10.99 | 2020-01-01 00:00:00 |
| 2  | Cheeseburger     | 8.99  | 2020-01-01 00:00:00 |
| 3  | French Fries     | 4.99  | 2020-01-01 00:00:00 |

The target table, which is currently empty, will be materialized with the following data:

| ID | Name             | Price |     Updated At      |     Valid From      | Valid To |
|----|------------------|:-----:|:-------------------:|:-------------------:|:--------:|
| 1  | Chicken Sandwich | 10.99 | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 |   NULL   |
| 2  | Cheeseburger     | 8.99  | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 |   NULL   |
| 3  | French Fries     | 4.99  | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 |   NULL   |

Now lets say that you update the source table with the following data:

| ID | Name             | Price |     Updated At      |
|----|------------------|:-----:|:-------------------:|
| 1  | Chicken Sandwich | 12.99 | 2020-01-02 00:00:00 |
| 3  | French Fries     | 4.99  | 2020-01-01 00:00:00 |
| 4  | Milkshake        | 3.99  | 2020-01-02 00:00:00 |

Summary of Changes:

* The price of the Chicken Sandwich was increased from $10.99 to $12.99.
* Cheeseburger was removed from the menu.
* Milkshakes were added to the menu.

Assuming your pipeline ran at `2020-01-02 11:00:00`, target table will be updated with the following data:

| ID | Name             | Price |     Updated At      |     Valid From      |      Valid To       |
|----|------------------|:-----:|:-------------------:|:-------------------:|:-------------------:|
| 1  | Chicken Sandwich | 10.99 | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 | 2020-01-02 00:00:00 |
| 1  | Chicken Sandwich | 12.99 | 2020-01-02 00:00:00 | 2020-01-02 00:00:00 |        NULL         |
| 2  | Cheeseburger     | 8.99  | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 | 2020-01-02 11:00:00 |
| 3  | French Fries     | 4.99  | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 |        NULL         |
| 4  | Milkshake        | 3.99  | 2020-01-02 00:00:00 | 2020-01-02 00:00:00 |        NULL         |

For our final pass, lets say that you update the source table with the following data:

| ID | Name                | Price |     Updated At      |
|----|---------------------|:-----:|:-------------------:|
| 1  | Chicken Sandwich    | 14.99 | 2020-01-03 00:00:00 |
| 2  | Cheeseburger        | 8.99  | 2020-01-03 00:00:00 |
| 3  | French Fries        | 4.99  | 2020-01-01 00:00:00 |
| 4  | Chocolate Milkshake | 3.99  | 2020-01-02 00:00:00 |

Summary of changes:

* The price of the Chicken Sandwich was increased from $12.99 to $14.99 (must be good!)
* Cheeseburger was added back to the menu with original name and price.
* Milkshake name was updated to be "Chocolate Milkshake".

Target table will be updated with the following data:

| ID | Name                | Price |     Updated At      |     Valid From      |      Valid To       |
|----|---------------------|:-----:|:-------------------:|:-------------------:|:-------------------:|
| 1  | Chicken Sandwich    | 10.99 | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 | 2020-01-02 00:00:00 |
| 1  | Chicken Sandwich    | 12.99 | 2020-01-02 00:00:00 | 2020-01-02 00:00:00 | 2020-01-03 00:00:00 |
| 1  | Chicken Sandwich    | 14.99 | 2020-01-03 00:00:00 | 2020-01-03 00:00:00 |        NULL         |
| 2  | Cheeseburger        | 8.99  | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 | 2020-01-02 11:00:00 |
| 2  | Cheeseburger        | 8.99  | 2020-01-03 00:00:00 | 2020-01-03 00:00:00 |        NULL         |
| 3  | French Fries        | 4.99  | 2020-01-01 00:00:00 | 1970-01-01 00:00:00 |        NULL         |
| 4  | Milkshake           | 3.99  | 2020-01-02 00:00:00 | 2020-01-02 00:00:00 | 2020-01-03 00:00:00 |
| 4  | Chocolate Milkshake | 3.99  | 2020-01-03 00:00:00 | 2020-01-03 00:00:00 |        NULL         |

**Note:** `Cheeseburger` was deleted from `2020-01-02 11:00:00` to `2020-01-03 00:00:00` meaning if you queried the table during that time range then you would not see `Cheeseburger` in the menu.
This is the most accurate representation of the menu based on the source data provided.
If `Cheeseburger` were added back to the menu with it's original updated at timestamp of `2020-01-01 00:00:00` then the `valid_from` timestamp of the new record would have been `2020-01-02 11:00:00` resulting in no period of time where the item was deleted.
Since in this case the updated at timestamp did not change it is likely the item was removed in error and this again most accurately represents the menu based on the source data.


### Example of SCD Type 2 By Column in Action

Lets say that you started with the following data in your source table and `invalidate_hard_deletes` is set to `true`:

| ID | Name             | Price |
|----|------------------|:-----:|
| 1  | Chicken Sandwich | 10.99 |
| 2  | Cheeseburger     | 8.99  |
| 3  | French Fries     | 4.99  |

We configure the SCD Type 2 By Column model to check the columns `Name` and `Price` for changes

The target table, which is currently empty, will be materialized with the following data:

| ID | Name             | Price |     Valid From      | Valid To |
|----|------------------|:-----:|:-------------------:|:--------:|
| 1  | Chicken Sandwich | 10.99 | 1970-01-01 00:00:00 |   NULL   |
| 2  | Cheeseburger     | 8.99  | 1970-01-01 00:00:00 |   NULL   |
| 3  | French Fries     | 4.99  | 1970-01-01 00:00:00 |   NULL   |

Now lets say that you update the source table with the following data:

| ID | Name             | Price |
|----|------------------|:-----:|
| 1  | Chicken Sandwich | 12.99 |
| 3  | French Fries     | 4.99  |
| 4  | Milkshake        | 3.99  |

Summary of Changes:

* The price of the Chicken Sandwich was increased from $10.99 to $12.99.
* Cheeseburger was removed from the menu.
* Milkshakes were added to the menu.

Assuming your pipeline ran at `2020-01-02 11:00:00`, target table will be updated with the following data:

| ID | Name             | Price |     Valid From      |      Valid To       |
|----|------------------|:-----:|:-------------------:|:-------------------:|
| 1  | Chicken Sandwich | 10.99 | 1970-01-01 00:00:00 | 2020-01-02 11:00:00 |
| 1  | Chicken Sandwich | 12.99 | 2020-01-02 11:00:00 |        NULL         |
| 2  | Cheeseburger     | 8.99  | 1970-01-01 00:00:00 | 2020-01-02 11:00:00 |
| 3  | French Fries     | 4.99  | 1970-01-01 00:00:00 |        NULL         |
| 4  | Milkshake        | 3.99  | 2020-01-02 11:00:00 |        NULL         |

For our final pass, lets say that you update the source table with the following data:

| ID | Name                | Price |
|----|---------------------|:-----:|
| 1  | Chicken Sandwich    | 14.99 |
| 2  | Cheeseburger        | 8.99  |
| 3  | French Fries        | 4.99  |
| 4  | Chocolate Milkshake | 3.99  |

Summary of changes:

* The price of the Chicken Sandwich was increased from $12.99 to $14.99 (must be good!)
* Cheeseburger was added back to the menu with original name and price.
* Milkshake name was updated to be "Chocolate Milkshake".

Assuming your pipeline ran at `2020-01-03 11:00:00`, Target table will be updated with the following data:

| ID | Name                | Price |     Valid From      |      Valid To       |
|----|---------------------|:-----:|:-------------------:|:-------------------:|
| 1  | Chicken Sandwich    | 10.99 | 1970-01-01 00:00:00 | 2020-01-02 11:00:00 |
| 1  | Chicken Sandwich    | 12.99 | 2020-01-02 11:00:00 | 2020-01-03 11:00:00 |
| 1  | Chicken Sandwich    | 14.99 | 2020-01-03 11:00:00 |        NULL         |
| 2  | Cheeseburger        | 8.99  | 1970-01-01 00:00:00 | 2020-01-02 11:00:00 |
| 2  | Cheeseburger        | 8.99  | 2020-01-03 11:00:00 |        NULL         |
| 3  | French Fries        | 4.99  | 1970-01-01 00:00:00 |        NULL         |
| 4  | Milkshake           | 3.99  | 2020-01-02 11:00:00 | 2020-01-03 11:00:00 |
| 4  | Chocolate Milkshake | 3.99  | 2020-01-03 11:00:00 |        NULL         |

**Note:** `Cheeseburger` was deleted from `2020-01-02 11:00:00` to `2020-01-03 11:00:00` meaning if you queried the table during that time range then you would not see `Cheeseburger` in the menu.
This is the most accurate representation of the menu based on the source data provided.

### Shared Configuration Options

| Name                    | Description                                                                                                                                                                                                                                                                                                       | Type                      |
|-------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------|
| unique_key              | Unique key used for identifying rows between source and target                                                                                                                                                                                                                                                    | List of strings or string |
| valid_from_name         | The name of the `valid_from` column to create in the target table. Default: `valid_from`                                                                                                                                                                                                                          | string                    |
| valid_to_name           | The name of the `valid_to` column to create in the target table. Default: `valid_to`                                                                                                                                                                                                                              | string                    |
| invalidate_hard_deletes | If set to `true`, when a record is missing from the source table it will be marked as invalid. Default: `false`                                                                                                                                                                                                   | bool                      |
| batch_size              | The maximum number of intervals that can be evaluated in a single backfill task. If this is `None`, all intervals will be processed as part of a single task. See [Processing Source Table with Historical Data](#processing-source-table-with-historical-data) for more info on this use case. (Default: `None`) | int                       |

!!! tip "Important"

    If using BigQuery, the default data type of the valid_from/valid_to columns is DATETIME. If you want to use TIMESTAMP, you can specify the data type in the model definition.

    ```sql linenums="1" hl_lines="5"
    MODEL (
      name db.menu_items,
      kind SCD_TYPE_2_BY_TIME (
        unique_key id,
        time_data_type TIMESTAMP
      )
    );
    ```

    This could likely be used on other engines to change the expected data type but has only been tested on BigQuery.

### SCD Type 2 By Time Configuration Options

| Name                     | Description                                                                                                                                                                        | Type                      |
|--------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------|
| updated_at_name          | The name of the column containing a timestamp to check for new or updated records. Default: `updated_at`                                                                           | string                    |
| updated_at_as_valid_from | By default, for new rows `valid_from` is set to `1970-01-01 00:00:00`. This changes the behavior to set it to the valid of `updated_at` when the row is inserted. Default: `false` | bool                      |

### SCD Type 2 By Column Configuration Options

| Name                         | Description                                                                                                                                                                                                                                                                                                                                  | Type                      |
|------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------|
| columns                      | The name of the columns to check for changes. `*` to represent that all columns should be checked.                                                                                                                                                                                                                                           | List of strings or string |
| execution_time_as_valid_from | By default, when the model is first loaded `valid_from` is set to `1970-01-01 00:00:00` and future new rows will have `execution_time` of when the pipeline ran. This changes the behavior to always use `execution_time`. Default: `false`                                                                                                  | bool                      |
| updated_at_name              | If sourcing from a table that includes as timestamp to use as valid_from, set this property to that column. See [Processing Source Table with Historical Data](#processing-source-table-with-historical-data) for more info on this use case. (Default: `None`) | int                       |


### Processing Source Table with Historical Data

The most common case for SCD Type 2 is creating history for a table that it doesn't have it already. 
In the example of the restaurant menu, the menu just tells you what is offered right now, but you want to know what was offered over time.
In this case, the default setting of `None` for `batch_size` is the best option.

Another use case though is processing a source table that already has history in it. 
A common example of this is a "daily snapshot" table that is created by a source system that takes a snapshot of the data at the end of each day.
If your source table has historical records, like a "daily snapshot" table, then set `batch_size` to `1` to process each interval (each day if a `@daily` cron) in sequential order.
That way the historical records will be properly captured in the SCD Type 2 table.

#### Example - Source from Daily Snapshot Table

```sql linenums="1"
MODEL (
    name db.table,
    kind SCD_TYPE_2_BY_COLUMN (
        unique_key id,
        columns [some_value],
        updated_at_name ds,
        batch_size 1
    ),
    start '2025-01-01',
    cron '@daily'
);
SELECT
    id,
    some_value,
    ds
FROM
    source_table
WHERE
    ds between @start_ds and @end_ds
```

This will process each day of the source table in sequential order (if more than one day to process), checking `some_value` column to see if it changed. If it did change, `valid_from` will be set to match the `ds` column (except for first value which would be `1970-01-01 00:00:00`).

If the source data was the following:

| id | some_value |     ds      |
|----|------------|:-----------:|
| 1  | 1          | 2025-01-01  |
| 1  | 2          | 2025-01-02  |
| 1  | 3          | 2025-01-03  |
| 1  | 3          | 2025-01-04  |

Then the resulting SCD Type 2 table would be:

| id | some_value |     ds      |     valid_from      |      valid_to       |
|----|------------|:-----------:|:-------------------:|:-------------------:|
| 1  | 1          | 2025-01-01  | 1970-01-01 00:00:00 | 2025-01-02 00:00:00 |
| 1  | 2          | 2025-01-02  | 2025-01-02 00:00:00 | 2025-01-03 00:00:00 |
| 1  | 3          | 2025-01-03  | 2025-01-03 00:00:00 |        NULL         |

### Querying SCD Type 2 Models

#### Querying the current version of a record

Although SCD Type 2 models support history, it is still very easy to query for just the latest version of a record. Simply query the model as you would any other table.
For example, if you wanted to query the latest version of the `menu_items` table you would simply run:

```sql linenums="1"
SELECT
  *
FROM
  menu_items
WHERE
  valid_to IS NULL;
```

One could also create a view on top of the SCD Type 2 model that creates a new `is_current` column to make it easy for consumers to identify the current record.

```sql linenums="1"
SELECT
  *,
  valid_to IS NULL AS is_current
FROM
  menu_items;
```

#### Querying for a specific version of a record at a give point in time

If you wanted to query the `menu_items` table as it was on `2020-01-02 01:00:00` you would simply run:

```sql linenums="1"
SELECT
  *
FROM
  menu_items
WHERE
  id = 1
  AND '2020-01-02 01:00:00' >= valid_from
  AND '2020-01-02 01:00:00' < COALESCE(valid_to, CAST('2199-12-31 23:59:59+00:00' AS TIMESTAMP));
```

Example in a join:

```sql linenums="1"
SELECT
  *
FROM
  orders
INNER JOIN
  menu_items
  ON orders.menu_item_id = menu_items.id
  AND orders.created_at >= menu_items.valid_from
  AND orders.created_at < COALESCE(menu_items.valid_to, CAST('2199-12-31 23:59:59+00:00' AS TIMESTAMP));
```

A view can be created to do the `COALESCE` automatically. This, combined with the `is_current` flag, makes it easier to query for a specific version of a record.

```sql linenums="1"
SELECT
  id,
  name,
  price,
  updated_at,
  valid_from,
  COALESCE(valid_to, CAST('2199-12-31 23:59:59+00:00' AS TIMESTAMP)) AS valid_to
  valid_to IS NULL AS is_current,
FROM
  menu_items;
```

Furthermore if you want to make it so users can use `BETWEEN` when querying by making `valid_to` inclusive you can do the following:
```sql linenums="1"
SELECT
  id,
  name,
  price,
  updated_at,
  valid_from,
  COALESCE(valid_to, CAST('2200-01-01 00:00:00+00:00' AS TIMESTAMP)) - INTERVAL 1 SECOND AS valid_to
  valid_to IS NULL AS is_current,
```

Note: The precision of the timestamps in this example is second so I subtract 1 second. Make sure to subtract a value equal to the precision of your timestamps.

#### Querying for deleted records

One way to identify deleted records is to query for records that do not have a `valid_to` record of `NULL`. For example, if you wanted to query for all deleted ids in the `menu_items` table you would simply run:

```sql linenums="1"
SELECT
  id,
  MAX(CASE WHEN valid_to IS NULL THEN 0 ELSE 1 END) AS is_deleted
FROM
  menu_items
GROUP BY
  id
```

### Reset SCD Type 2 Model (clearing history)

SCD Type 2 models are designed by default to protect the data that has been captured because it is not possible to recreate the history once it has been lost.
However, there are cases where you may want to clear the history and start fresh.
For this use case you will want to start by setting `disable_restatement` to `false` in the model definition.

```sql linenums="1" hl_lines="5"
MODEL (
  name db.menu_items,
  kind SCD_TYPE_2_BY_TIME (
    unique_key id,
    disable_restatement false
  )
);
```

Plan/apply this change to production.
Then you will want to [restate the model](../plans.md#restatement-plans).

```bash
sqlmesh plan --restate-model db.menu_items
```

!!! warning

    This will remove the historical data on the model which in most situations cannot be recovered.

Once complete you will want to remove `disable_restatement` on the model definition which will set it back to `true` and prevent accidental data loss.

```sql linenums="1"
MODEL (
  name db.menu_items,
  kind SCD_TYPE_2_BY_TIME (
    unique_key id,
  )
);
```

Plan/apply this change to production.

## EXTERNAL

The EXTERNAL model kind is used to specify [external models](./external_models.md) that store metadata about external tables. External models are special; they are not specified in .sql files like the other model kinds. They are optional but useful for propagating column and type information for external tables queried in your SQLMesh project.

## MANAGED

!!! warning

    Managed models are still under development and the API / semantics may change as support for more engines is added

**Note:** Python models do not support the `MANAGED` model kind - use a SQL model instead.

The `MANAGED` model kind is used to create models where the underlying database engine manages the data lifecycle.

These models don't get updated with new intervals or refreshed when `sqlmesh run` is called. Responsibility for keeping the *data* up to date falls on the engine.

You can control how the engine creates the managed model by using the [`physical_properties`](../overview#physical_properties-previously-table_properties) to pass engine-specific parameters for adapter to use when issuing commands to the underlying database.

Due to there being no standard, each vendor has a different implementation with different semantics and different configuration parameters. Therefore, `MANAGED` models are not as portable between database engines as other SQLMesh model types. In addition, due to their black-box nature, SQLMesh has limited visibility into the integrity and state of the model.

We would recommend using standard SQLMesh model types in the first instance. However, if you do need to use Managed models, you still gain other SQLMesh benefits like the ability to use them in [virtual environments](../../concepts/overview#build-a-virtual-environment).

See [Managed Models](./managed_models.md) for more information on which engines are supported and which properties are available.

## INCREMENTAL_BY_PARTITION

Models of the `INCREMENTAL_BY_PARTITION` kind are computed incrementally based on partition. A set of columns defines the model's partitioning key, and a partition is the group of rows with the same partitioning key value.

!!! question "Should you use this model kind?"

    Any model kind can use a partitioned **table** by specifying the [`partitioned_by` key](../models/overview.md#partitioned_by) in the `MODEL` DDL.

    The "partition" in `INCREMENTAL_BY_PARTITION` is about how the data is **loaded** when the model runs.

    `INCREMENTAL_BY_PARTITION` models are inherently [non-idempotent](../glossary.md#idempotency), so restatements and other actions can cause data loss. This makes them more complex to manage than other model kinds.

    In most scenarios, an `INCREMENTAL_BY_TIME_RANGE` model can meet your needs and will be easier to manage. The `INCREMENTAL_BY_PARTITION` model kind should only be used when the data must be loaded by partition (usually for performance reasons).

This model kind is designed for the scenario where data rows should be loaded and updated as a group based on their shared value for the partitioning key.

It may be used with any SQL engine. SQLMesh will automatically create partitioned tables on engines that support explicit table partitioning (e.g., [BigQuery](https://cloud.google.com/bigquery/docs/creating-partitioned-tables), [Databricks](https://docs.databricks.com/en/sql/language-manual/sql-ref-partition.html)).

New rows are loaded based on their partitioning key value:

- If a partitioning key in newly loaded data is not present in the model table, the new partitioning key and its data rows are inserted.
- If a partitioning key in newly loaded data is already present in the model table, **all the partitioning key's existing data rows in the model table are replaced** with the partitioning key's data rows in the newly loaded data.
- If a partitioning key is present in the model table but not present in the newly loaded data, the partitioning key's existing data rows are not modified and remain in the model table.

This kind should only be used for datasets that have the following traits:

* The dataset's records can be grouped by a partitioning key.
* Each record has a partitioning key associated with it.
* It is appropriate to upsert records, so existing records can be overwritten by new arrivals when their partitioning keys match.
* All existing records associated with a given partitioning key can be removed or overwritten when any new record has the partitioning key value.

The column defining the partitioning key is specified in the model's `MODEL` DDL `partitioned_by` key. This example shows the `MODEL` DDL for an `INCREMENTAL_BY_PARTITION` model whose partition key is the row's value for the `region` column:

```sql linenums="1" hl_lines="4"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_PARTITION,
  partitioned_by region,
);
```

Compound partition keys are also supported, such as `region` and `department`:

```sql linenums="1" hl_lines="4"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_PARTITION,
  partitioned_by (region, department),
);
```

Date and/or timestamp column expressions are also supported (varies by SQL engine). This BigQuery example's partition key is based on the month each row's `event_date` occurred:

```sql linenums="1" hl_lines="4"
MODEL (
  name db.events,
  kind INCREMENTAL_BY_PARTITION,
  partitioned_by DATETIME_TRUNC(event_date, MONTH)
);
```

!!! warning "Only full restatements supported"

    Partial data [restatements](../plans.md#restatement-plans) are used to reprocess part of a table's data (usually a limited time range).

    Partial data restatement is not supported for `INCREMENTAL_BY_PARTITION` models. If you restate an `INCREMENTAL_BY_PARTITION` model, its entire table will be recreated from scratch.

    Restating `INCREMENTAL_BY_PARTITION` models may lead to data loss and should be performed with care.

### Example

This is a fuller example of how you would use this model kind in practice. It limits the number of partitions to backfill based on time range in the `partitions_to_update` CTE.

```sql linenums="1"
MODEL (
  name demo.incremental_by_partition_demo,
  kind INCREMENTAL_BY_PARTITION,
  partitioned_by user_segment,
);

-- This is the source of truth for what partitions need to be updated and will join to the product usage data
-- This could be an INCREMENTAL_BY_TIME_RANGE model that reads in the user_segment values last updated in the past 30 days to reduce scope
-- Use this strategy to reduce full restatements
WITH partitions_to_update AS (
  SELECT DISTINCT
    user_segment
  FROM demo.incremental_by_time_range_demo  -- upstream table tracking which user segments to update
  WHERE last_updated_at BETWEEN DATE_SUB(@start_dt, INTERVAL 30 DAY) AND @end_dt
),

product_usage AS (
  SELECT
    product_id,
    customer_id,
    last_usage_date,
    usage_count,
    feature_utilization_score,
    user_segment
  FROM sqlmesh-public-demo.tcloud_raw_data.product_usage
  WHERE user_segment IN (SELECT user_segment FROM partitions_to_update) -- partition filter applied here
)

SELECT
  product_id,
  customer_id,
  last_usage_date,
  usage_count,
  feature_utilization_score,
  user_segment,
  CASE
    WHEN usage_count > 100 AND feature_utilization_score > 0.7 THEN 'Power User'
    WHEN usage_count > 50 THEN 'Regular User'
    WHEN usage_count IS NULL THEN 'New User'
    ELSE 'Light User'
  END as user_type
FROM product_usage
```

**Note**: Partial data [restatement](../plans.md#restatement-plans) is not supported for this model kind, which means that the entire table will be recreated from scratch if restated. This may lead to data loss.

### Materialization strategy
Depending on the target engine, models of the `INCREMENTAL_BY_PARTITION` kind are materialized using the following strategies:

| Engine     | Strategy                                |
|------------|-----------------------------------------|
| Databricks | REPLACE WHERE by partitioning key       |
| Spark      | INSERT OVERWRITE by partitioning key    |
| Snowflake  | DELETE by partitioning key, then INSERT |
| BigQuery   | DELETE by partitioning key, then INSERT |
| Redshift   | DELETE by partitioning key, then INSERT |
| Postgres   | DELETE by partitioning key, then INSERT |
| DuckDB     | DELETE by partitioning key, then INSERT |

## INCREMENTAL_UNMANAGED

The `INCREMENTAL_UNMANAGED` model kind exists to support append-only tables. It's "unmanaged" in the sense that SQLMesh doesnt try to manage how the data is loaded. SQLMesh will just run your query on the configured cadence and append whatever it gets into the table.

!!! question "Should you use this model kind?"

    Some patterns for data management, such as Data Vault, may rely on append-only tables. In this situation, `INCREMENTAL_UNMANAGED` is the correct type to use.

    In most other situations, you probably want `INCREMENTAL_BY_TIME_RANGE` or `INCREMENTAL_BY_UNIQUE_KEY` because they give you much more control over how the data is loaded.

Usage of the `INCREMENTAL_UNMANAGED` model kind is straightforward:

```sql linenums="1" hl_lines="3"
MODEL (
  name db.events,
  kind INCREMENTAL_UNMANAGED,
);
```

Since it's unmanaged, it doesnt support the `batch_size` and `batch_concurrency` properties to control how data is loaded like the other incremental model types do.

!!! warning "Only full restatements supported"

    Similar to `INCREMENTAL_BY_PARTITION`, attempting to [restate](../plans.md#restatement-plans) an `INCREMENTAL_UNMANAGED` model will trigger a full restatement. That is, the model will be rebuilt from scratch rather than from a time slice you specify.

    This is because an append-only table is inherently non-idempotent. Restating `INCREMENTAL_UNMANAGED` models may lead to data loss and should be performed with care.
