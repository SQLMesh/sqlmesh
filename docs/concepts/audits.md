# Auditing
Audits are one of the tools SQLMesh provides to validate your models. Along with [tests](tests.md), they are a great way to ensure the quality of your data and to build trust in it across your organization.

Unlike tests, audits are used to validate the output of a model after every run. When you apply a [plan](./plans.md), SQLMesh will automatically run each model's audits.

By default, SQLMesh will halt plan application when an audit fails so potentially invalid data does not propagate further downstream. This behavior can be changed for individual audits - refer to [Non-blocking audits](#non-blocking-audits).

A comprehensive suite of audits can identify data issues upstream, whether they are from your vendors or other teams. Audits also empower your data engineers and analysts to work with confidence by catching problems early as they work on new features or make updates to your models.

**NOTE**: For incremental by time range models, audits are only applied to intervals being processed - not for the entire underlying table.

## Blocking audits
A failed blocking audit halts the execution of a `plan` or `run` to prevent invalid data from propagating to downstream models. The impact of a failure depends on whether you are running a `plan` or a `run`.

SQLMesh's blocking audit process is:

1. Evaluate the model (e.g., insert new data or rebuild the table)
2. Run the audit query against the newly updated model table. For incremental models, the audit only runs on the processed time intervals.
3. If the query returns any rows, the audit fails, halting the `plan` or `run`.

### Plan vs. Run

The key difference is when the model's data is promoted to the production environment:

*   **`plan`**: SQLMesh evaluates and audits all modified models *before* promoting them to production. If an audit fails, the `plan` stops, and the production table is untouched. Invalid data is contained in an isolated table and never reaches the production environment.

*   **`run`**: SQLMesh evaluates and audits models directly against the production environment. If an audit fails, the `run` stops, but the invalid data *is already present* in the production table. The "blocking" action prevents this bad data from being used to build other downstream models.

### Fixing a Failed Audit

If a blocking audit fails during a `run`, you must fix the invalid data in the production table. To do so:

1.  **Find the root cause**: examine upstream models and data sources
2.  **Fix the source**
    *   If the cause is an **external data source**, fix it there. Then, run a [restatement plan](./plans.md#restatement-plans) on the first SQLMesh model that ingests the source data. This will restate all downstream models, including the one with the failed audit.
    *   If the cause is a **SQLMesh model**, update the model's logic. Then apply the change with a `plan`, which will automatically re-evaluate all downstream models.

## User-Defined Audits
In SQLMesh, user-defined audits are defined in `.sql` files in an `audits` directory in your SQLMesh project. Multiple audits can be defined in a single file, so you can organize them to your liking. Alternatively, audits can be defined inline within the model definition itself.

Audits are SQL queries that should not return any rows; in other words, they query for bad data, so returned rows indicates that something is wrong.

In its simplest form, an audit is defined with the `AUDIT` statement along with a query, as in the following example:

```sql linenums="1"
AUDIT (
  name assert_item_price_is_not_null,
  dialect spark
);
SELECT * from sushi.items
WHERE
  ds BETWEEN @start_ds AND @end_ds
  AND price IS NULL;
```

In the example, we defined an audit named `assert_item_price_is_not_null`, ensuring that every sushi item has a price.

**Note:** If the query is in a different dialect than the rest of your project, you can specify it in the `AUDIT` statement. In the example above we set it to `spark`, so SQLGlot will automatically understand how to execute the query behind the scenes.

To run the audit, include it in a model's `MODEL` statement:

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (assert_item_price_is_not_null)
);
```

Now `assert_item_price_is_not_null` will run every time the `sushi.items` model is run.

### Generic audits
Audits can also be parameterized and implemented in a model-agnostic way so the same audit can be used for multiple models.

Consider the following audit definition that checks whether the target column exceeds a configured threshold:

```sql linenums="1"
AUDIT (
  name does_not_exceed_threshold
);
SELECT * FROM @this_model
WHERE @column >= @threshold;
```

This example utilizes [macros](./macros/overview.md) to parameterize the audit. `@this_model` is a special macro which refers to the model that is being audited. For incremental models, this macro also ensures that only relevant data intervals are affected.

`@column` and `@threshold` are parameters whose values are specified in a model definition's `MODEL` statement.

Apply the generic audit to a model by referencing it in the `MODEL` statement:

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (
    does_not_exceed_threshold(column := id, threshold := 1000),
    does_not_exceed_threshold(column := price, threshold := 100)
  )
);
```

Notice how `column` and `threshold` parameters have been set. These values will be propagated into the audit query and substituted into the `@column` and `@threshold` macro variables.

Note that the same audit can be applied more than once to the a model using different sets of parameters.

Generic audits can define default values as follows:
```sql linenums="1"
AUDIT (
  name does_not_exceed_threshold,
  defaults (
    threshold = 10,
    column = id
  )
);
SELECT * FROM @this_model
WHERE @column >= @threshold;
```

Alternatively, you can apply specific audits globally by including them in the model defaults configuration:

```sql linenums="1"
model_defaults:
  audits:
    - assert_positive_order_ids
    - does_not_exceed_threshold(column := id, threshold := 1000)
```

### Naming
We recommended avoiding SQL keywords when naming audit parameters. Quote any audit argument that is also a SQL keyword.

For example, if an audit `my_audit` uses a `values` parameter, invoking it will require quotes because `values` is a SQL keyword:

```sql linenums="1" hl_lines="4"
MODEL (
  name sushi.items,
  audits (
    my_audit(column := a, "values" := (1,2,3))
  )
)
```

### Inline audits

You can also define audits directly within a model definition using the same syntax. Multiple audits can be specified within the same SQL model file:


```sql linenums="1"
MODEL (
    name sushi.items,
    audits(does_not_exceed_threshold(column := id, threshold := 1000), price_is_not_null)
);
SELECT id, price
FROM sushi.seed;

AUDIT (name does_not_exceed_threshold);
SELECT * FROM @this_model
WHERE @column >= @threshold;

AUDIT (name price_is_not_null);
SELECT * FROM @this_model
WHERE price IS NULL;
```
### Standalone audits

Standalone audits are defined independently rather than being attached to a specific model. They specify the models they depend on using the `depends_on` property.

Unlike model-level audits, standalone audits can be used to validate data across one or more models without being associated with a single model.

Standalone audits run as scheduled nodes during both `sqlmesh plan` and `sqlmesh run`.

```sql linenums="1"
AUDIT (
  name assert_item_price_is_not_null,
  dialect spark,
  standalone TRUE,
  depends_on (
    sushi.items
  )
);

SELECT *
FROM sushi.items
WHERE
  ds BETWEEN @start_ds AND @end_ds
  AND price IS NULL;
```

In this example, the audit checks that the `price` column in `sushi.items` does not contain `NULL` values for the selected date range.

Standalone audits can declare dependencies using the `depends_on` property. SQLMesh can often infer dependencies directly from the audit query, but using `depends_on` is recommended when inference isn't sufficient.

!!! note

    Standalone audits are non-blocking only. Because they are not associated with a single model, SQLMesh cannot determine which model should be blocked if the audit fails.
## Built-in audits
SQLMesh comes with a suite of built-in generic audits that cover a broad set of common use cases. Built-in audits are blocking by default, but they all have non-blocking counterparts which you can use by appending `_non_blocking` - see [Non-blocking audits](#non-blocking-audits).

This section describes the audits, grouped by general purpose.

### Generic assertion audit

The `forall` audit is the most generic built-in audit, allowing arbitrary boolean SQL expressions.

#### forall, forall_non_blocking
Ensures that a set of arbitrary boolean expressions evaluate to `TRUE` for all rows in the model. The boolean expressions should be written in SQL.

This example asserts that all rows have a `price` greater than 0 and a `name` value containing at least one character:

```sql linenums="1" hl_lines="4-7"
MODEL (
  name sushi.items,
  audits (
    forall(criteria := (
      price > 0,
      LENGTH(name) > 0
    ))
  )
);
```

### Row counts and NULL value audits

These audits concern row counts and presence of `NULL` values.

#### number_of_rows, number_of_rows_non_blocking
Ensures that the number of rows in the model's table exceeds the threshold.

This example asserts that the model has more than 10 rows:

```sql linenums="1"
MODEL (
  name sushi.orders,
  audits (
    number_of_rows(threshold := 10)
  )
);
```

#### not_null, not_null_non_blocking
Ensures that specified columns do not contain `NULL` values.

This example asserts that none of the `id`, `customer_id`, or `waiter_id` columns contain `NULL` values:

```sql linenums="1"
MODEL (
  name sushi.orders,
  audits (
    not_null(columns := (id, customer_id, waiter_id))
  )
);
```

#### at_least_one, at_least_one_non_blocking
Ensures that specified columns contain at least one non-NULL value.

This example asserts that the `zip` column contains at least one non-NULL value:

```sql linenums="1"
MODEL (
  name sushi.customers,
  audits (
    at_least_one(column := zip)
    )
);
```

#### not_null_proportion, not_null_proportion_non_blocking
Ensures that the specified column's proportion of `NULL` values is no greater than a threshold.

This example asserts that the `zip` column has no more than 80% `NULL` values:

```sql linenums="1"
MODEL (
  name sushi.customers,
  audits (
    not_null_proportion(column := zip, threshold := 0.8)
    )
);
```

### Specific data values audits

These audits concern the specific set of data values present in a column.

#### not_constant, not_constant_non_blocking
Ensures that the specified columns are not constant (i.e., have at least two non-NULL values).

This example asserts that the column `customer_id` has at least two non-NULL values:

```sql linenums="1"
MODEL (
  name sushi.customer_revenue_by_day,
  audits (
    not_constant(column := customer_id)
    )
);
```

#### unique_values, unique_values_non_blocking
Ensures that specified columns contain unique values (i.e., have no duplicated values).

This example asserts that the `id` and `item_id` columns have unique values:

```sql linenums="1"
MODEL (
  name sushi.orders,
  audits (
    unique_values(columns := (id, item_id))
  )
);
```

#### unique_combination_of_columns, unique_combination_of_columns_non_blocking
Ensures that each row has a unique combination of values over the specified columns.

This example asserts that the combination of `id` and `ds` columns has no duplicated values:

```sql linenums="1"
MODEL (
  name sushi.orders,
  audits (
    unique_combination_of_columns(columns := (id, ds))
  )
);
```

#### accepted_values, accepted_values_non_blocking
Ensures that all rows of the specified column contain one of the accepted values.

!!! note
    Rows with `NULL` values for the column will pass this audit in most databases/engines. Use the [`not_null` audit](#not_null) to ensure there are no `NULL` values present in a column.

This example asserts that column `name` has a value of 'Hamachi', 'Unagi', or 'Sake':

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (
    accepted_values(column := name, is_in := ('Hamachi', 'Unagi', 'Sake'))
  )
);
```

#### not_accepted_values, not_accepted_values_non_blocking
Ensures that no rows of the specified column contain one of the not accepted values.

!!! note
    This audit does not support rejecting `NULL` values. Use the [`not_null` audit](#not_null) to ensure there are no `NULL` values present in a column.

This example asserts that column `name` is not one of 'Hamburger' or 'French fries':

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (
    not_accepted_values(column := name, is_in := ('Hamburger', 'French fries'))
  )
);
```

### Numeric data audits

These audits concern the distribution of values in numeric columns.

#### sequential_values, sequential_values_non_blocking
Ensures that each of an ordered numeric column's values contains the previous row's value plus `interval`.

For example, with a column having minimum value 1 and maximum value 4 and `interval := 1`, it ensures that the rows contain values `[1, 2, 3, 4]`.

This example asserts that column `item_id` contains sequential values that differ by `1`:

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (
    sequential_values(column := item_id, interval := 1)
  )
);
```

#### accepted_range, accepted_range_non_blocking
Ensures that a column's values are in a numeric range. Range is inclusive by default, such that values equal to the range boundaries will pass the audit.

This example asserts that all rows have a `price` greater than or equal 1 and less than or equal to 100:

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (
    accepted_range(column := price, min_v := 1, max_v := 100)
  )
);
```

This example specifies the `inclusive := false` argument to assert that all rows have a `price` greater than 0 and less than 100:

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (
    accepted_range(column := price, min_v := 0, max_v := 100, inclusive := false)
  )
);
```

#### mutually_exclusive_ranges, mutually_exclusive_ranges_non_blocking
Ensures that each row's numeric range does not overlap with any other row's range.

This example asserts that each row's range [min_price, max_price] does not overlap with any other row's range:

```sql linenums="1"
MODEL (
  audits (
    mutually_exclusive_ranges(lower_bound_column := min_price, upper_bound_column := max_price)
  )
);
```

### Character data audits

These audits concern the characteristics of values in character/string columns.

!!! warning
    Databases/engines may exhibit different behavior for different character sets or languages.

#### not_empty_string, not_empty_string_non_blocking
Ensures that no rows of a column contain an empty string value `''`.

This example asserts that no `name` is an empty string:

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (
    not_empty_string(column := name)
  )
);
```

#### string_length_equal, string_length_equal_non_blocking
Ensures that all rows of a column contain a string with the specified number of characters.

This example asserts that all `zip` values are 5 characters long:

```sql linenums="1"
MODEL (
  name sushi.customers,
  audits (
    string_length_equal(column := zip, v := 5)
    )
);
```

#### string_length_between, string_length_between_non_blocking
Ensures that all rows of a column contain a string with number of characters in the specified range. Range is inclusive by default, such that values equal to the range boundaries will pass the audit.

This example asserts that all `name` values have 5 or more and 50 or fewer characters:

```sql linenums="1"
MODEL (
  name sushi.customers,
  audits (
    string_length_between(column := name, min_v := 5, max_v := 50)
    )
);
```

This example specifies the `inclusive := false` argument to assert that all rows have a `name` with 5 or more and 59 or fewer characters:

```sql linenums="1"
MODEL (
  name sushi.customers,
  audits (
    string_length_between(column := zip, min_v := 4, max_v := 60, inclusive := false)
    )
);
```

#### valid_uuid, valid_uuid_non_blocking
Ensures that all non-NULL rows of a column contain a string with the UUID structure.

UUID structure determined by matching regular expression `'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'`.

This example asserts that all `uuid` values have the UUID structure:

```sql linenums="1"
MODEL (
  audits (
    valid_uuid(column := uuid)
    )
);
```

#### valid_email, valid_email_non_blocking
Ensures that all non-NULL rows of a column contain a string with the email address structure.

Email address structure determined by matching regular expression `'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'`.

This example asserts that all `email` values have the email address structure:

```sql linenums="1"
MODEL (
  audits (
    valid_email(column := email)
    )
);
```

#### valid_url, valid_url_non_blocking
Ensures that all non-NULL rows of a column contain a string with the URL structure.

URL structure determined by matching regular expression `'^(https?|ftp)://[^\s/$.?#].[^\s]*$'`.

This example asserts that all `url` values have the URL structure:

```sql linenums="1"
MODEL (
  audits (
    valid_url(column := url)
    )
);
```

#### valid_http_method, valid_http_method_non_blocking
Ensures that all non-NULL rows of a column contain a valid HTTP method.

Valid HTTP methods determined by matching values `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`, `TRACE`, `CONNECT`.

This example asserts that all `http_method` values are valid HTTP methods:

```sql linenums="1"
MODEL (
  audits (
    valid_http_method(column := http_method)
    )
);
```

#### match_regex_pattern_list, match_regex_pattern_list_non_blocking
Ensures that all non-NULL rows of a column match at least one of the specified regular expressions.

This example asserts that all `todo` values match one of `'^\d.*'` (string starts with a digit) or `'.*!$'` (ends with an exclamation mark):

```sql linenums="1"
MODEL (
  audits (
    match_regex_pattern_list(column := todo, patterns := ('^\d.*', '.*!$'))
  )
);
```

#### not_match_regex_pattern_list, not_match_regex_pattern_list_non_blocking
Ensures that no non-NULL rows of a column match any of the specified regular expressions.

This example asserts that no `todo` values match one of `'^!.*'` (string starts with an exclamation mark) or `'.*\d$'` (ends with a digit):

```sql linenums="1"
MODEL (
  audits (
    match_regex_pattern_list(column := todo, patterns := ('^!.*', '.*\d$'))
  )
);
```

#### match_like_pattern_list, match_like_pattern_list_non_blocking
Ensures that all non-NULL rows of a column are `LIKE` at least one of the specified patterns.

This example asserts that all `name` values are `LIKE` one of `'jim%'` or `'pam%'`:

```sql linenums="1"
MODEL (
  audits (
    match_like_pattern_list(column := name, patterns := ('jim%', 'pam%'))
  )
);
```

#### not_match_like_pattern_list, not_match_like_pattern_list_non_blocking
Ensures that no non-NULL rows of a column are `LIKE` any of the specified patterns.

This example asserts that no `name` values are `LIKE` `'%doe'` or `'%smith'`:

```sql linenums="1"
MODEL (
  audits (
    not_match_like_pattern_list(column := name, patterns := ('%doe', '%smith'))
  )
);
```


### Statistical audits

These audits concern the statistical distributions of numeric columns.

!!! note

    Audit thresholds will likely require fine-tuning via trial and error for each column being audited.

#### mean_in_range, mean_in_range_non_blocking
Ensures that a numeric column's mean is in the specified range. Range is inclusive by default, such that values equal to the range boundaries will pass the audit.

This example asserts that the `age` column has a mean of at least 21 and at most 50:

```sql linenums="1"
MODEL (
  audits (
    mean_in_range(column := age, min_v := 21, max_v := 50)
    )
);
```

This example specifies the `inclusive := false` argument to assert that `age` has a mean greater than 18 and less than 65:

```sql linenums="1"
MODEL (
  audits (
    mean_in_range(column := age, min_v := 18, max_v := 65, inclusive := false)
    )
);
```

#### stddev_in_range, stddev_in_range_non_blocking
Ensures that a numeric column's standard deviation is in the specified range. Range is inclusive by default, such that values equal to the range boundaries will pass the audit.

This example asserts that the `age` column has a standard deviation of at least 2 and at most 5:

```sql linenums="1"
MODEL (
  audits (
    stddev_in_range(column := age, min_v := 2, max_v := 5)
    )
);
```

This example specifies the `inclusive := false` argument to assert that `age` has a standard deviation greater than 3 and less than 6:

```sql linenums="1"
MODEL (
  audits (
    mean_in_range(column := age, min_v := 3, max_v := 6, inclusive := false)
    )
);
```

#### z_score, z_score_non_blocking
Ensures that no rows of a numeric column contain a value whose absolute z-score exceeds the threshold.

z-score is calculated as `ABS(([row value] - [column mean]) / NULLIF([column standard deviation], 0))`.

This example asserts that the `age` column contains no rows with z-scores greater than 3:

```sql linenums="1"
MODEL (
  audits (
    z_score(column := age, threshold := 3)
    )
);
```

#### kl_divergence, kl_divergence_non_blocking
Ensures that the [symmetrised Kullback-Leibler divergence](https://en.wikipedia.org/wiki/Kullback%E2%80%93Leibler_divergence#Symmetrised_divergence) (aka "Jeffreys divergence" or "Population Stability Index") between two columns does not exceed a threshold.

This example asserts that the symmetrised KL Divergence between columns `age` and `reference_age` is less than or equal to 0.1:

```sql linenums="1"
MODEL (
  audits (
    kl_divergence(column := age, target_column := reference_age, threshold := 0.1)
    )
);
```

#### chi_square, chi_square_non_blocking
Ensures that the [chi-square](https://en.wikipedia.org/wiki/Chi-squared_test) statistic for two categorical columns does not exceed a critical value.

You can look up the critical value corresponding to a p-value with a table (such as [this one](https://www.medcalc.org/manual/chi-square-table.php)) or by using the Python [scipy library](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.chi2.html):

```python linenums="1"
from scipy.stats import chi2

# critical value for p-value := 0.95 and degrees of freedom := 1
chi2.ppf(0.95, 1)
```
This example asserts that the chi-square statistic0 for columns `user_state` and `user_type` does not exceed 6.635:

```sql linenums="1"
MODEL (
  audits (
    chi_square(column := user_state, target_column := user_type, critical_value := 6.635)
    )
);
```

## Running audits
### The CLI audit command

You can execute audits with the `sqlmesh audit` command as follows:

```bash
$ sqlmesh -p project audit --start 2022-01-01 --end 2022-01-02
Found 1 audit(s).
assert_item_price_is_not_null FAIL.

Finished with 1 audit error(s).

Failure in audit assert_item_price_is_not_null for model sushi.items (audits/items.sql).
Got 3 results, expected 0.
SELECT * FROM sqlmesh.sushi__items__1836721418_83893210 WHERE ds BETWEEN '2022-01-01' AND '2022-01-02' AND price IS NULL
Done.
```

### Automated auditing
When you apply a plan, SQLMesh will automatically run each model's audits.

By default, SQLMesh will halt the pipeline when an audit fails to prevent potentially invalid data from propagating further downstream. This behavior can be changed for individual audits - see [Non-blocking audits](#non-blocking-audits).

## Advanced usage
### Skipping audits
Audits can be skipped by setting the `skip` argument to `true` as in the following example:

```sql linenums="1" hl_lines="3"
AUDIT (
  name assert_item_price_is_not_null,
  skip true
);
SELECT * from sushi.items
WHERE ds BETWEEN @start_ds AND @end_ds AND
   price IS NULL;
```

### Non-blocking audits
By default, audits that fail will stop the execution of the pipeline to prevent bad data from propagating further.

An audit can be configured to notify you without blocking the execution of the pipeline when it fails, as in this example:

```sql linenums="1" hl_lines="3"
AUDIT (
  name assert_item_price_is_not_null,
  blocking false
);
SELECT * from sushi.items
WHERE ds BETWEEN @start_ds AND @end_ds AND
   price IS NULL;
```

The `blocking` property can also be set at the use-site of an audit, using the special `blocking` argument:

```sql linenums="1"
MODEL (
  name sushi.items,
  audits (
    does_not_exceed_threshold(column := id, threshold := 1000, blocking := false),
    does_not_exceed_threshold(column := price, threshold := 100, blocking := false)
  )
);
```
