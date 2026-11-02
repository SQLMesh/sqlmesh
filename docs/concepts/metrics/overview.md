# Overview

Metrics are currently in a prototype phase and not meant for production use.

SQLMesh provides a framework for defining and working with metrics (also known as a semantic layer). Metrics are arbitrary SQL functions that perform aggregations for use in analytics, data science, or machine learning.

A semantic layer is valuable because it provides a consistent definition and language for metrics. For example, if an executive asks "How many active users are there?" the answer could differ depending on who they ask or what dashboard they look at. The tables, aggregations, and joins needed to correctly calculate the answer could be complex and implemented differently (or incorrectly) by different people.

Metrics are defined in a SQLMesh project metrics directory, and they are selected by name in a model query. SQLMesh uses its semantic understanding of the query to determine the metric's role in the query, determine the appropriate SQL operations to calculate the metric, and add them to the query code submitted to the SQL engine.

## Example

### Definition

Metrics are defined using SQLMesh's SQL-based [metrics definition language](definition.md).

The following is an example metric definition. Note the following aspects:

- The metrics `expression` can be any aggregate SQL function (`COUNT` in this example)
- The columns referenced in `expression` are fully qualified (`sushi.customers.status`)
- The metric can reference multiple models as long as their [grains](../models/overview.md#grain)/[references](../models/overview.md#references) are properly specified (`expression` uses both the `sushi.customers` and `sushi.orders` models)

```sql linenums="1"
METRIC (
  name total_orders_from_active_customers,
  expression COUNT(IF(sushi.customers.status = 'ACTIVE', sushi.orders.id, NULL))
);
```

### Querying

SQLMesh models access metrics in their query's `SELECT` clause with the `METRIC` function and the metric name.

For example, this model query selects the `total_orders_from_active_customer` metric. Because it is a simple query that solely selects a metric and its grouping column, it can select from the special table `__semantic.__table`:

```sql linenums="1"
SELECT
  ds,
  METRIC(total_orders_from_active_customers), -- METRIC function
FROM __semantic.__table  -- special table for simple metric queries
GROUP BY ds
```

SQLMesh expands the metric into SQL equivalent to the following query (generated aliases may differ):

``` sql linenums="1"
SELECT
  __table.ds AS ds,
  __table.total_orders_from_active_customers AS total_orders_from_active_customers
FROM (
  SELECT
    sushi__orders.ds,
    COUNT(CASE WHEN sushi__customers.status = 'ACTIVE' THEN sushi__orders.id ELSE NULL END) AS total_orders_from_active_customers
  FROM sushi.orders AS sushi__orders
  LEFT JOIN sushi.customers AS sushi__customers
    ON sushi__orders.customer_id = sushi__customers.customer_id
  GROUP BY
    sushi__orders.ds
) AS __table
```

SQLMesh automatically generates the correct join to use values from both the `sushi.orders` and `sushi.customers` tables.

## Filters and grouping across metrics

Queries against `__semantic.__table` treat its columns as logical dimensions. A `WHERE` predicate is resolved independently for every contributing fact source and applied before aggregation, including when the filtered dimension is not in `GROUP BY`. Derived metrics are calculated from those filtered aggregates.

Dimension resolution prefers a column on the fact itself. Otherwise, the column must resolve to one reachable model through the configured grains and references. An explicit dimension-table alias selects that model instead of a same-named fact column. Unknown, unreachable, or ambiguous dimensions are rejected rather than ignored.

Fact aggregates are combined with a full join by default. Group keys present only in a later fact are retained, and matching `NULL` group keys are combined, including composite keys. Missing metric values remain `NULL`; they are not automatically converted to zero.

On PostgreSQL, full joins use composite-key equality to retain `NULL` groups without the planner restriction on `IS NOT DISTINCT FROM` join conditions. Corresponding dimensions must have matching PostgreSQL types; use an explicit cast in the grouping expression when models expose different types. Metric arithmetic follows the definition's SQL dialect: use a numeric or floating-point cast for fractional ratios of integer counts, and `NULLIF(denominator, 0)` when a zero denominator should produce `NULL`.

The prototype rejects subqueries in metric `WHERE` filters, grouping sets, and reference paths that cannot be compiled into safe matching-key joins. This includes multi-hop paths that change reference keys. These checks do not restrict ordinary SQL scopes without `METRIC` expressions. They are not a substitute for application authorization.
