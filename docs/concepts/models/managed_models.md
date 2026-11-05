# Managed models
Some database engines support tables whose data is kept up to date by the engine itself, rather than by the user. These tables are typically defined by a query that reads from other tables in the database. When the source tables are updated, the engine automatically updates the managed table to reflect the changes, without requiring the user to issue a `REFRESH` command.

Each supported database engine handles these updates differently, but most use background processes to keep the tables up to date according to the parameters you specify when creating the table.

SQLMesh exposes this functionality through managed models for supported engines. Setting a model's kind to `MANAGED` tells SQLMesh that the database engine is responsible for keeping the data up to date and that SQLMesh only needs to maintain the schema. Managed models are therefore typically built on an [external model](./external_models.md) rather than another SQLMesh model.

!!! warning "Not supported in Python models"

  Python models do not support the `MANAGED` [model kind](./model_kinds.md). Use a SQL model instead.

## Difference from materialized views
The difference between an Managed model and a materialized view is down to semantics and in some engines there is no difference.

SQLMesh has support for [materialized views](../model_kinds#materialized-views) already. However, depending on the engine, these are subject to some limitations, such as:

- A Materialized View query can only be derived from a single base table
- The Materialized View is not automatically maintained by the engine. To refresh the data, a `REFRESH MATERIALIZED VIEW` or equivalent command must be issued

Managed models are different in that:

- The engine updates the table data automatically when a base table changes
- When performing updates, the engine has a semantic understanding of the query and can decide if an incremental or full refresh should be applied
- There is no need to issue manual `REFRESH` commands. The engine maintains the table transparently in a background process

## Lifecycle in SQLMesh
Managed models follow the same lifecycle as other models:

- Creating a Virtual Environment creates a pointer to the current model snapshot
- Modifying the model causes a new snapshot to be created
- Any upstream changes cause a new snapshot to be created
- The model can be deployed and rolled back via the usual pointer swap mechanism
- Once the TTL expires, model snapshots are cleaned up

However, there is usually extra vendor-imposed costs associated with Managed models. For example, Snowflake has [additional costs](https://docs.snowflake.com/en/user-guide/dynamic-tables-cost) for Dynamic Tables.

Therefore, we try to not create managed tables unnecessarily. For example, in [forward-only plans](../plans.md#forward-only-change) we just create a normal table to preview the changes and only re-create the managed table on deployment to prod.

!!! warning
    Due to the use of normal tables for dev previews, it is possible to write a query that uses features that are available to normal tables in the target engine but not managed tables. This could result in a scenario where a plan works in a dev environment but fails when deployed to production.

    We believe the cost savings are worth it, however please [reach out](https://tobikodata.com/slack) if this causes problems for you.

## Supported Engines
SQLMesh supports managed models in the following database engines:

| Engine                                               | Implementatation                                                               |
| ---------------------------------------------------- | ------------------------------------------------------------------------------ |
| [Snowflake](../../integrations/engines/snowflake.md) | [Dynamic Table](https://docs.snowflake.com/en/user-guide/dynamic-tables-intro) |

To define a managed model, you can use the [`MANAGED`](./model_kinds.md#managed) model Kind.

### Snowflake

Managed Models are in Snowflake are implemented as [Dynamic Tables](https://docs.snowflake.com/en/user-guide/dynamic-tables-intro).

Here is an example of a SQLMesh model that will result in a dynamic table being created:

```sql linenums="1"
MODEL (
  name db.events,
  kind MANAGED,
  physical_properties (
    warehouse = datalake,
    target_lag = '2 minutes',
    data_retention_time_in_days = 2
  )
);

SELECT
  event_date::DATE as event_date,
  event_payload::TEXT as payload
FROM raw_events
```

results in:

```sql linenums="1"
CREATE OR REPLACE DYNAMIC TABLE db.events
  WAREHOUSE = "datalake",
  TARGET_LAG = '2 minutes'
  DATA_RETENTION_TIME_IN_DAYS = 2
AS SELECT
  event_date::DATE as event_date,
  event_payload::TEXT as payload
FROM raw_events
```

!!! note

    SQLMesh will not create intervals and run this model for each interval, so there is no need to add a WHERE clause with date filters like you would for a normal incremental model. How the data in this model is refreshed is completely up to Snowflake.

#### Table properties

Dynamic Tables have some properties that affect things like how often the data is refreshed by Snowflake, when the initial data is populated, how long data is retained for etc. The list of available properties is located in the [Snowflake documentation](https://docs.snowflake.com/sql-reference/sql/create-dynamic-table).

In SQLMesh, these properties are set on the model definition.

The following Dynamic Table properties are set on the model [`physical_properties`](../models/overview.md#physical_properties-previously-table_properties):

| Snowflake Property              | Required | Notes
| ------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| target_lag                      | Y        |                                                                                                                                         |
| warehouse                       | N        | In Snowflake, this is a required property. However, if not specified, then SQLMesh will use the result of `select current_warehouse()`. |
| refresh_mode                    | N        |                                                                                                                                         |
| initialize                      | N        |                                                                                                                                         |
| data_retention_time_in_days     | N        |                                                                                                                                         |
| max_data_extension_time_in_days | N        |                                                                                                                                         |

The following Dynamic Table properties can be set directly on the model:

| Snowflake Property | Required   | Notes                                                                                                                                                                   |
| ------------------ | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| cluster by         | N          | `clustered_by` is a [standard model property](../models/overview.md#clustered_by), so set `clustered_by` on the model to add a `CLUSTER BY` clause to the Dynamic Table |