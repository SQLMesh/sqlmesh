# Definition

Metrics are defined in SQL files in the `metrics/` directory of your SQLMesh project. A single file can contain multiple metric definitions.

A metric is defined with the function `METRIC()` and must include the `name` and `expression` keys. The `name` is case insensitive and must be unique, and the `expression` contains the SQL code used to calculate the metric.

## SQL expression
The expression field can be any SQL statement that contains an aggregation function (e.g., `MAX`, `SUM`). This example uses the `COUNT` function:

```sql linenums="1"
METRIC (
  name unique_account_ids,
  expression COUNT(DISTINCT silver.accounts.account_id)
);
```

All columns referenced in the expression should be fully qualified with the model name. For example, if a model name is `a.b`, a metric referencing column `c` in that model should refer to it as `a.b.c`.

The example above refers to the `account_id` column in model `silver.accounts` using `silver.accounts.account_id`.

### Automatic joins
Metrics can refer to multiple tables and will use the model [grains](../models/overview.md#grain) and [references](../models/overview.md#references) to automatically join them together. `grains` specify a model's key column(s) that uniquely identify the model's rows and `references` specify column(s) that other tables may join to.

For example, a SQLMesh project might contain the `prod.users` and `prod.searches` models with the following `MODEL` DDLs.

The `prod.users` model has a grain of `user_id`, meaning that its rows are uniquely identified by the `user_id` column:

```sql linenums="1"
MODEL (
  name prod.users,
  grain user_id
);
```

The `prod.searches` model specifies `user_id` in its `references` key, signaling that other models may join to its `user_id` column:

```sql linenums="1"
MODEL (
  name prod.searches,
  grain search_id,
  references user_id,
);
```

Because those models specify their grain and references, SQLMesh can correctly generate code for a metric that uses columns from both models.

In this example, `canadian_searchers` sums searches from users located in Canada:

```sql linenums="1"
METRIC (
  name canadian_searchers,
  expression SUM(IF(prod.users.country = 'CAD', prod.searches.num_searches, 0)),
);
```

Because the `prod.users.country` and `prod.searches.num_searches` models have specified their grains/references, SQLMesh can automatically do the correct join between them.

### Derived metrics

Metrics can perform additional operations/calculations with other metrics.

In this example, the third metric `clicks_per_search` is calculated by dividing `total_clicks` by `total_searches`:

```sql linenums="1"
METRIC (
  name total_searches,
  expression SUM(prod.searchers.num_searches)
);

METRIC (
  name total_clicks,
  expression SUM(prod.clickers.num_clicks)
);

METRIC (
  name clicks_per_search,
  expression total_clicks / total_searches -- Calculated from the other two metrics
);
```

Metric references are case insensitive, including quoted references, and may refer to metrics defined later in the project. Shared dependencies are resolved once during expansion. An unknown dependency or a dependency cycle raises a configuration error with the dependency chain and the path of the definition containing the invalid reference.

## Properties

The `METRIC` definition supports the following keys. The `name` and `expression` keys are required.

### name
- The name of the metric. This name is case insensitive and must be unique in a project.

### expression
- A SQL expression consisting of an aggregation, a formula consisting of other metrics, or a combination of both.

### description
- The description of the metric.

### owner
- The owner of the metric. Used for organizational and governance purposes.

### dialect
- The dialect that the expression is written in. It is recommended to leave this empty and rely on the project's default dialect.
