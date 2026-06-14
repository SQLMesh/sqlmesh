# SQLMesh macros

## Macro systems: two approaches

SQLMesh macros behave differently than those of templating systems like [Jinja](https://jinja.palletsprojects.com/en/3.1.x/).

Macro systems are based on string substitution. The macro system scans code files, identifies special characters that signify macro content, and replaces the macro elements with other text.

In a general sense, that is the entire functionality of templating systems. They have tools that provide control flow logic (if-then) and other functionality, but *that functionality is solely to support substituting in the correct strings*.

Templating systems are intentionally agnostic to the programming language being templated, and most of them work for everything from blog posts to HTML to SQL.

In contrast, SQLMesh macros are designed specifically for generating SQL code. They have *semantic understanding* of the SQL code being created by analyzing it with the Python [sqlglot](https://github.com/tobymao/sqlglot) library, and they allow use of Python code so users can tidily implement sophisticated macro logic.

### SQLMesh macro approach

This section describes how SQLMesh macros work under the hood. Feel free to skip over this section and return if and when it is useful. This information is **not** required to use SQLMesh macros, but it will be useful for debugging any macros exhibiting puzzling behavior.

The critical distinction between the SQLMesh macro approach and templating systems is the role string substitution plays. In templating systems, string substitution is the entire and only point.

In SQLMesh, string substitution is just one step toward modifying the semantic representation of the SQL query. *SQLMesh macros work by building and modifying the semantic representation of the SQL query.*

After processing all the non-SQL text, it uses the substituted values to modify the semantic representation of the query to its final state.

It uses the following five step approach to accomplish this:

1. Parse the text with the appropriate sqlglot SQL dialect (e.g., Postgres, BigQuery, etc.). During the parsing, it detects the special macro symbol `@` to differentiate non-SQL from SQL text. The parser builds a semantic representation of the SQL code's structure, capturing non-SQL text as "placeholder" values to use in subsequent steps.

2. Examine the placeholder values to classify them as one of the following types:

    - Creation of user-defined macro variables with the `@DEF` operator (see more about [user-defined macro variables](#user-defined-variables))
    - Macro variables: [SQLMesh pre-defined](./macro_variables.md), [user-defined local](#local-variables), and [user-defined global](#global-variables)
    - Macro functions, both [SQLMesh's](#sqlmesh-macro-operators) and [user-defined](#user-defined-macro-functions)

3. Substitute macro variable values where they are detected. In most cases, this is direct string substitution as with a templating system.

4. Execute any macro functions and substitute the returned values.

5. Modify the semantic representation of the SQL query with the substituted variable values from (3) and functions from (4).

### Embedding variables in strings

SQLMesh always incorporates macro variable values into the semantic representation of a SQL query (step 5 above). To do that, it infers the role each macro variable value plays in the query.

For context, two commonly used types of string in SQL are:

- String literals, which represent text values and are surrounded by single quotes, such as `'the_string'`
- Identifiers, which reference database objects like column, table, alias, and function names
    - They may be unquoted or quoted with double quotes, backticks, or brackets, depending on the SQL dialect

In a normal query, SQLMesh can easily determine which role a given string is playing. However, it is more difficult if a macro variable is embedded directly into a string - especially if the string is in the `MODEL` block (and not the query itself).

For example, consider a project that defines a [gateway variable](#gateway-variables) named `gateway_var`. The project includes a model that references `@gateway_var` as part of the schema in the model's `name`, which is a SQL *identifier*.

This is how we might try to write the model:

``` sql title="Incorrectly rendered to string literal"
MODEL (
  name the_@gateway_var_schema.table
);
```

From SQLMesh's perspective, the model schema is the combination of three sub-strings: `the_`, the value of `@gateway_var`, and `_schema`.

SQLMesh will concatenate those strings, but it does not have the context to know that it is building a SQL identifier and will return a string literal.

To provide the context SQLMesh needs, you must add curly braces to the macro variable reference: `@{gateway_var}` instead of `@gateway_var`:

``` sql title="Correctly rendered to identifier"
MODEL (
  name the_@{gateway_var}_schema.table
);
```

The curly braces let SQLMesh know that it should treat the string as a SQL identifier, which it will then quote based on the SQL dialect's quoting rules.

The most common use of the curly brace syntax is embedding macro variables into strings, it can also be used to differentiate string literals and identifiers in SQL queries. For example, consider a macro variable `my_variable` whose value is `col`.

If we `SELECT` this value with regular macro syntax, it will render to a string literal:

``` sql
SELECT @my_variable AS the_column; -- renders to SELECT 'col' AS the_column
```

`'col'` is surrounded with single quotes, and the SQL engine will use that string as the column's data value.

If we use curly braces, SQLMesh will know that we want to use the rendered string as an identifier:

``` sql
SELECT @{my_variable} AS the_column; -- renders to SELECT col AS the_column
```

`col` is not surrounded with single quotes, and the SQL engine will determine that the query is referencing a column or other object named `col`.

## User-defined variables

SQLMesh supports four kinds of user-defined macro variables: [global](#global-variables), [gateway](#gateway-variables), [blueprint](#blueprint-variables) and [local](#local-variables).

Global and gateway macro variables are defined in the project configuration file and can be accessed in any project model. Blueprint and macro variables are defined in a model definition and can only be accessed in that model.

Macro variables with the same name may be specified at any or all of the global, gateway, blueprint and local levels. When variables are specified at multiple levels, the value of the most specific level takes precedence. For example, the value of a local variable takes precedence over the value of a blueprint or gateway variable with the same name, and the value of a gateway variable takes precedence over the value of a global variable.

### Global variables

Global variables are defined in the project configuration file [`variables` key](../../reference/configuration.md#variables).

Global variable values may be any of the following data types or lists or dictionaries containing these types: `int`, `float`, `bool`, `str`.

Access global variable values in a model definition using the `@<VAR_NAME>` macro or the `@VAR()` macro function. The latter function requires the name of the variable _in single quotes_ as the first argument and an optional default value as the second argument. The default value is a safety mechanism used if the variable name is not found in the project configuration file.

For example, this SQLMesh configuration key defines six variables of different data types:

=== "YAML"

    ```yaml linenums="1"
    variables:
      int_var: 1
      float_var: 2.0
      bool_var: true
      str_var: "cat"
      list_var: [1, 2, 3]
      dict_var:
        key1: 1
        key2: 2
    ```

=== "Python"

    ``` python linenums="1"
    variables = {
        "int_var": 1,
        "float_var": 2.0,
        "bool_var": True,
        "str_var": "cat",
        "list_var": [1, 2, 3],
        "dict_var": {"key1": 1, "key2": 2},
    }

    config = Config(
        variables=variables,
        ... # other Config arguments
    )
    ```

A model definition could access the `int_var` value in a `WHERE` clause like this:

```sql linenums="1"
SELECT *
FROM table
WHERE int_variable = @INT_VAR
```

Alternatively, the same variable can be accessed by passing the variable name into the `@VAR()` macro function. Note that the variable name is in single quotes in the call `@VAR('int_var')`:

```sql linenums="1"
SELECT *
FROM table
WHERE int_variable = @VAR('int_var')
```

A default value can be passed as a second argument to the `@VAR()` macro function, which will be used as a fallback value if the variable is missing from the configuration file.

In this example, the `WHERE` clause would render to `WHERE some_value = 0` because no variable named `missing_var` was defined in the project configuration file:

```sql linenums="1"
SELECT *
FROM table
WHERE some_value = @VAR('missing_var', 0)
```

A similar API is available for [Python macro functions](#accessing-global-variable-values) via the `evaluator.var` method and [Python models](../models/python_models.md#global-variables) via the `context.var` method.

### Gateway variables

Like global variables, gateway variables are defined in the project configuration file. However, they are specified in a specific gateway's `variables` key:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        variables:
          int_var: 1
        ...
    ```

=== "Python"

    ``` python linenums="1"
    gateway_variables = {
      "int_var": 1
    }

    config = Config(
        gateways={
          "my_gateway": GatewayConfig(
            variables=gateway_variables
            ... # other GatewayConfig arguments
            ),
          }
    )
    ```

Access them in models using the same methods as [global variables](#global-variables).

Gateway-specific variable values take precedence over variables with the same name specified in the root `variables` key.

### Blueprint variables

Blueprint macro variables are defined in a model. Blueprint variable values take precedence over [global](#global-variables) or [gateway-specific](#gateway-variables) variables with the same name.

Blueprint variables are defined as a property of the `MODEL` statement, and serve as a mechanism for [creating model templates](../models/sql_models.md):

```sql linenums="1"
MODEL (
  name @customer.some_table,
  kind FULL,
  blueprints (
    (customer := customer1, field_a := x, field_b := y, field_c := 'foo'),
    (customer := customer2, field_a := z, field_b := w, field_c := 'bar')
  )
);

SELECT
  @field_a,
  @{field_b} AS field_b,
  @field_c AS @{field_c}
FROM @customer.some_source

/*
When rendered for customer1.some_table:
SELECT
  x,
  y AS field_b,
  'foo' AS foo
FROM customer1.some_source

When rendered for customer2.some_table:
SELECT
  z,
  w AS field_b,
  'bar' AS bar
FROM customer2.some_source
*/
```

Note the use of both regular `@field_a` and curly brace syntax `@{field_b}` macro variable references in the model query. Both of these will be rendered as identifiers. In the case of `field_c`, which in the blueprints is a string, it would be rendered as a string literal when used with the regular macro syntax `@field_c` and if we want to use the string as an identifier then we use the curly braces `@{field_c}`. Learn more [above](#embedding-variables-in-strings)

Blueprint variables can be accessed using the syntax shown above, or through the `@BLUEPRINT_VAR()` macro function, which also supports specifying default values in case the variable is undefined (similar to `@VAR()`).

### Local variables

Local macro variables are defined in a model. Local variable values take precedence over [global](#global-variables), [blueprint](#blueprint-variables), or [gateway-specific](#gateway-variables) variables with the same name.

Define your own local macro variables with the `@DEF` macro operator. For example, you could set the macro variable `macro_var` to the value `1` with:

```sql linenums="1"
@DEF(macro_var, 1);
```

SQLMesh has three basic requirements for using the `@DEF` operator:

1. The `MODEL` statement must end with a semi-colon `;`
2. All `@DEF` uses must come after the `MODEL` statement and before the SQL query
3. Each `@DEF` use must end with a semi-colon `;`

For example, consider the following model `sqlmesh_example.full_model` from the [SQLMesh quickstart guide](../../quick_start.md):

```sql linenums="1"
MODEL (
  name sqlmesh_example.full_model,
  kind FULL,
  cron '@daily',
  audits (assert_positive_order_ids),
);

SELECT
  item_id,
  count(distinct id) AS num_orders,
FROM
  sqlmesh_example.incremental_model
GROUP BY item_id
```

This model could be extended with a user-defined macro variable to filter the query results based on `item_size` like this:

```sql linenums="1"
MODEL (
  name sqlmesh_example.full_model,
  kind FULL,
  cron '@daily',
  audits (assert_positive_order_ids),
); -- NOTE: semi-colon at end of MODEL statement

@DEF(size, 1); -- NOTE: semi-colon at end of @DEF operator

SELECT
  item_id,
  count(distinct id) AS num_orders,
FROM
  sqlmesh_example.incremental_model
WHERE
  item_size > @size -- Reference to macro variable `@size` defined above with `@DEF()`
GROUP BY item_id
```

This example defines the macro variable `size` with `@DEF(size, 1)`. When the model is run, SQLMesh will substitute in the number `1` where `@size` appears in the `WHERE` clause.

### Macro functions

In addition to inline user-defined variables, SQLMesh also supports inline macro functions. These functions can be used to express more readable and reusable logic than is possible with variables alone. Lets look at an example:

```sql linenums="1"
MODEL(...);

@DEF(
  rank_to_int,
  x -> case when left(x, 1) = 'A' then 1 when left(x, 1) = 'B' then 2 when left(x, 1) = 'C' then 3 end
);

SELECT
  id,
  cust_rank_1,
  cust_rank_2,
  cust_rank_3
  @rank_to_int(cust_rank_1) as cust_rank_1_int,
  @rank_to_int(cust_rank_2) as cust_rank_2_int,
  @rank_to_int(cust_rank_3) as cust_rank_3_int
FROM
  some.model
```

Multiple arguments can be expressed in a macro function as well:

```sql linenums="1"
@DEF(pythag, (x,y) -> sqrt(pow(x, 2) + pow(y, 2)));

SELECT
  sideA,
  sideB,
  @pythag(sideA, sideB) AS sideC
FROM
  some.triangle
```

```sql linenums="1"
@DEF(nrr, (starting_mrr, expansion_mrr, churned_mrr) -> (starting_mrr + expansion_mrr - churned_mrr) / starting_mrr);

SELECT
  @nrr(fy21_mrr, fy21_expansions, fy21_churns) AS fy21_net_retention_rate,
  @nrr(fy22_mrr, fy22_expansions, fy22_churns) AS fy22_net_retention_rate,
  @nrr(fy23_mrr, fy23_expansions, fy23_churns) AS fy23_net_retention_rate,
FROM
  some.revenue
```

You can nest macro functions like so:

```sql linenums="1"
MODEL (
  name dummy.model,
  kind FULL
);

@DEF(area, r -> pi() * r * r);
@DEF(container_volume, (r, h) -> @area(@r) * h);

SELECT container_id, @container_volume((cont_di / 2), cont_hi) AS volume
```

## Macro operators

SQLMesh's macro system has multiple operators that allow different forms of dynamic behavior in models.

### @EACH

`@EACH` is used to transform a list of items by applying a function to each of them, analogous to a `for` loop.

??? info "Learn more about `for` loops and `@EACH`"

    Before diving into the `@EACH` operator, let's dissect a `for` loop to understand its components.

    `for` loops have two primary parts: a collection of items and an action that should be taken for each item. For example, here is a `for` loop in Python:

    ```python linenums="1"
    for number in [4, 5, 6]:
        print(number)
    ```

    This for loop prints each number present in the brackets:

    ```python linenums="1"
    4
    5
    6
    ```

    The first line of the example sets up the loop, doing two things:

    1. Telling Python that code inside the loop will refer to each item as `number`
    2. Telling Python to step through the list of items in brackets

    The second line tells Python what action should be taken for each item. In this case, it prints the item.

    The loop executes one time for each item in the list, substituting in the item for the word `number` in the code. For example, the first time through the loop the code would execute as `print(4)` and the second time as `print(5)`.

    The SQLMesh `@EACH` operator is used to implement the equivalent of a `for` loop in SQLMesh macros.

    `@EACH` gets its name from the fact that a loop performs the action "for each" item in the collection. It is fundamentally equivalent to the Python loop above, but you specify the two loop components differently.

`@EACH` takes two arguments: a list of items and a function definition.

```sql linenums="1"
@EACH([list of items], [function definition])
```

The function definition is specified inline. This example specifies the identity function, returning the input unmodified:

```sql linenums="1"
SELECT
  @EACH([4, 5, 6], number -> number)
FROM table
```

The loop is set up by the first argument: `@EACH([4, 5, 6]` tells SQLMesh to step through the list of items in brackets.

The second argument `number -> number` tells SQLMesh what action should be taken for each item using an "anonymous" function (aka "lambda" function). The left side of the arrow states what name the code on the right side will refer to each item as (like `name` in `for [name] in [items]` in a Python `for` loop).

The right side of the arrow specifies what should be done to each item in the list. `number -> number` tells `@EACH` that for each item `number` it should return that item (e.g., `1`).

SQLMesh macros use their semantic understanding of SQL code to take automatic actions based on where in a SQL query macro variables are used. If `@EACH` is used in the `SELECT` clause of a SQL statement:

1. It prints the item
2. It knows fields are separated by commas in `SELECT`, so it automatically separates the printed items with commas

Because of the automatic print and comma-separation, the anonymous function `number -> number` tells `@EACH` that for each item `number` it should print the item and separate the items with commas. Therefore, the complete output from the example is:

```sql linenums="1"
SELECT
  4,
  5,
  6
FROM table
```

This basic example is too simple to be useful. Many uses of `@EACH` will involve using the values as one or both of a literal value and an identifier.

For example, a column `favorite_number` in our data might contain values `4`, `5`, and `6`, and we want to unpack that column into three indicator (i.e., binary, dummy, one-hot encoded) columns. We could write that by hand as:

```sql linenums="1"
SELECT
  CASE WHEN favorite_number = 4 THEN 1 ELSE 0 END as favorite_4,
  CASE WHEN favorite_number = 5 THEN 1 ELSE 0 END as favorite_5,
  CASE WHEN favorite_number = 6 THEN 1 ELSE 0 END as favorite_6
FROM table
```

In that SQL query each number is being used in two distinct ways. For example, `4` is being used:

1. As a literal numeric value in `favorite_number = 4`
2. As part of a column name in `favorite_4`

We describe each of these uses separately.

For the literal numeric value, `@EACH` substitutes in the exact value that is passed in the brackets, *including quotes*. For example, consider this query similar to the `CASE WHEN` example above:

```sql linenums="1"
SELECT
  @EACH([4,5,6], x -> CASE WHEN favorite_number = x THEN 1 ELSE 0 END as column)
FROM table
```

It renders to this SQL:

```sql linenums="1"
SELECT
  CASE WHEN favorite_number = 4 THEN 1 ELSE 0 END AS column,
  CASE WHEN favorite_number = 5 THEN 1 ELSE 0 END AS column,
  CASE WHEN favorite_number = 6 THEN 1 ELSE 0 END AS column
FROM table
```

Note that the number `4`, `5`, and `6` are unquoted in *both* the input `@EACH` array in brackets and the resulting SQL query.

We can instead quote them in the input `@EACH` array:

```sql linenums="1"
SELECT
  @EACH(['4','5','6'], x -> CASE WHEN favorite_number = x THEN 1 ELSE 0 END as column)
FROM table
```

And they will be quoted in the resulting SQL query:

```sql linenums="1"
SELECT
  CASE WHEN favorite_number = '4' THEN 1 ELSE 0 END AS column,
  CASE WHEN favorite_number = '5' THEN 1 ELSE 0 END AS column,
  CASE WHEN favorite_number = '6' THEN 1 ELSE 0 END AS column
FROM table
```

We can place the array values at the end of a column name by using the SQLMesh macro operator `@` inside the `@EACH` function definition:

```sql linenums="1"
SELECT
  @EACH(['4','5','6'], x -> CASE WHEN favorite_number = x THEN 1 ELSE 0 END as column_@x)
FROM table
```

This query will render to:

```sql linenums="1"
SELECT
  CASE WHEN favorite_number = '4' THEN 1 ELSE 0 END AS column_4,
  CASE WHEN favorite_number = '5' THEN 1 ELSE 0 END AS column_5,
  CASE WHEN favorite_number = '6' THEN 1 ELSE 0 END AS column_6
FROM table
```

This syntax works regardless of whether the array values are quoted or not.

!!! note "Embedding macros in strings"

    SQLMesh macros support placing macro values at the end of a column name using `column_@x`.

    However, if you wish to substitute the variable anywhere else in the identifier, you need to use the more explicit curly brace syntax `@{}` to avoid ambiguity. For example, these are valid uses: `@{x}_column` or `my_@{x}_column`.

    Learn more about embedding macros in strings [above](#embedding-variables-in-strings)

### @IF

SQLMesh's `@IF` macro allows components of a SQL query to change based on the result of a logical condition.

It includes three elements:

1. A logical condition that evaluates to `TRUE` or `FALSE`
2. A value to return if the condition is `TRUE`
3. A value to return if the condition is `FALSE` [optional]

These elements are specified as:

```sql linenums="1"
@IF([logical condition], [value if TRUE], [value if FALSE])
```

The value to return if the condition is `FALSE` is optional - if it is not provided and the condition is `FALSE`, then the macro has no effect on the resulting query.

The logical condition should be written *in SQL* and is evaluated with [SQLGlot's](https://github.com/tobymao/sqlglot) SQL executor. It supports the following operators:

- Equality: `=` for equals, `!=` or `<>` for not equals
- Comparison: `<`, `>`, `<=`, `>=`,
- Between: `[number] BETWEEN [low number] AND [high number]`
- Membership: `[item] IN ([comma-separated list of items])`

For example, the following simple conditions are all valid SQL and evaluate to `TRUE`:

- `'a' = 'a'`
- `'a' != 'b'`
- `0 < 1`
- `1 >= 1`
- `2 BETWEEN 1 AND 3`
- `'a' IN ('a', 'b')`

`@IF` can be used to modify any part of a SQL query. For example, this query conditionally includes `sensitive_col` in the query results:

```sql linenums="1"
SELECT
  col1,
  @IF(1 > 0, sensitive_col)
FROM table
```

Because `1 > 0` evaluates to `TRUE`, the query is rendered as:

```sql linenums="1"
SELECT
  col1,
  sensitive_col
FROM table
```

Note that `@IF(1 > 0, sensitive_col)` does not include the third argument specifying a value if `FALSE`. Had the condition evaluated to `FALSE`, `@IF` would return nothing and only `col1` would be selected.

Alternatively, we could specify that `nonsensitive_col` be returned if the condition evaluates to `FALSE`:

```sql linenums="1"
SELECT
  col1,
  @IF(1 > 2, sensitive_col, nonsensitive_col)
FROM table
```

Because `1 > 2` evaluates to `FALSE`, the query is rendered as:

```sql linenums="1"
SELECT
  col1,
  nonsensitive_col
FROM table
```

[Macro rendering](#sqlmesh-macro-approach) occurs before the `@IF` condition is evaluated. For example, SQLMesh doesn't evaluate the condition `my_column > @my_value` until it has first substituted the number `@my_value` represents.

Your macro might do things besides returning a value, such as printing a message or executing a statement (i.e., the macro "has side effects"). The side effect code will always run during the rendering step. To prevent this, modify the macro code to condition the side effects on the evaluation stage.

#### Pre/post-statements

`@IF` may be used to conditionally execute pre/post-statements:

```sql linenums="1"
@IF([logical condition], [statement to execute if TRUE]);
```

The `@IF` statement itself must end with a semi-colon, but the inner statement argument must not.

This example conditionally executes a pre/post-statement depending on the model's [runtime stage](./macro_variables.md#predefined-variables), accessed via the pre-defined macro variable `@runtime_stage`. The `@IF` post-statement will only be executed at model evaluation time:

```sql linenums="1" hl_lines="17-20"
MODEL (
  name sqlmesh_example.full_model,
  kind FULL,
  cron '@daily',
  grain item_id,
  audits (assert_positive_order_ids),
);

SELECT
  item_id,
  count(distinct id) AS num_orders,
FROM
  sqlmesh_example.incremental_model
GROUP BY item_id
ORDER BY item_id;

@IF(
  @runtime_stage = 'evaluating',
  ALTER TABLE sqlmesh_example.full_model ALTER item_id TYPE VARCHAR
);
```

NOTE: alternatively, we could alter a column's type if the `@runtime_stage = 'creating'`, but that would only be useful if the model is incremental and the alteration would persist. `FULL` models are rebuilt on each evaluation, so changes made at their creation stage will be overwritten each time the model is evaluated.

### @EVAL

`@EVAL` evaluates its arguments with SQLGlot's SQL executor.

It allows you to execute mathematical or other calculations in SQL code. It behaves similarly to the first argument of the [`@IF` operator](#if), but it is not limited to logical conditions.

For example, consider a query adding 5 to a macro variable:

```sql linenums="1"
MODEL (
  ...
);

@DEF(x, 1);

SELECT
  @EVAL(5 + @x) as my_six
FROM table
```

After macro variable substitution, this would render as `@EVAL(5 + 1)` and be evaluated to `6`, resulting in the final rendered query:

```sql linenums="1"
SELECT
  6 as my_six
FROM table
```

### @FILTER

`@FILTER` is used to subset an input array of items to only those meeting the logical condition specified in the anonymous function. Its output can be consumed by other macro operators such as [`@EACH`](#each) or [`@REDUCE`](#reduce).

The user-specified anonymous function must evaluate to `TRUE` or `FALSE`. `@FILTER` applies the function to each item in the array, only including the item in the output array if it meets the condition.

The anonymous function should be written *in SQL* and is evaluated with [SQLGlot's](https://github.com/tobymao/sqlglot) SQL executor. It supports standard SQL equality and comparison operators - see [`@IF`](#if) above for more information about supported operators.

For example, consider this `@FILTER` call:

```sql linenums="1"
@FILTER([1,2,3], x -> x > 1)
```

It applies the condition `x > 1` to each item in the input array `[1,2,3]` and returns `[2,3]`.

### @REDUCE

`@REDUCE` is used to combine the items in an array.

The anonymous function specifies how the items in the input array should be combined. In contrast to `@EACH` and `@FILTER`, the anonymous function takes two arguments whose values are named in parentheses.

For example, an anonymous function for `@EACH` might be specified `x -> x + 1`. The `x` to the left of the arrow tells SQLMesh that the array items will be referred to as `x` in the code to the right of the arrow.

Because the `@REDUCE` anonymous function takes two arguments, the text to the left of the arrow must contain two comma-separated names in parentheses. For example, `(x, y) -> x + y` tells SQLMesh that items will be referred to as `x` and `y` in the code to the right of the arrow.

Even though the anonymous function takes only two arguments, the input array can contain as many items as necessary.

Consider the anonymous function `(x, y) -> x + y`. Conceptually, only the `y` argument corresponds to items in the array; the `x` argument is a temporary value created when the function is evaluated.

For the call `@REDUCE([1,2,3,4], (x, y) -> x + y)`, the anonymous function is applied to the array in the following steps:

1. Take the first two items in the array as `x` and `y`. Apply the function to them: `1 + 2` = `3`.
2. Take the output of step (1) as `x` and the next item in the array `3` as `y`. Apply the function to them: `3 + 3` = `6`.
3. Take the output of step (2) as `x` and the next item in the array `4` as `y`. Apply the function to them: `6 + 4` = `10`.
4. No items remain. Return value from step (3): `10`.

`@REDUCE` will almost always be used with another macro operator. For example, we might want to build a `WHERE` clause from multiple column names:

```sql linenums="1"
SELECT
  my_column
FROM
  table
WHERE
  col1 = 1 and col2 = 1 and col3 = 1
```

We can use `@EACH` to build each column's predicate (e.g., `col1 = 1`) and `@REDUCE` to combine them into a single statement:

```sql linenums="1"
SELECT
  my_column
FROM
  table
WHERE
  @REDUCE(
    @EACH([col1, col2, col3], x -> x = 1), -- Builds each individual predicate `col1 = 1`
    (x, y) -> x AND y -- Combines individual predicates with `AND`
  )
```

### @STAR

`@STAR` is used to return a set of column selections in a query.

`@STAR` is named after SQL's star operator `*`, but it allows you to programmatically generate a set of column selections and aliases instead of just selecting all available columns. A query may use more than one `@STAR` and may also include explicit column selections.

`@STAR` uses SQLMesh's knowledge of each table's columns and data types to generate the appropriate column list.

If the column data types are known, the resulting query `CAST`s columns to their data type in the source table. Otherwise, the columns will be listed without any casting.

`@STAR` supports the following arguments, in this order:

- `relation`: The relation/table whose columns are being selected
- `alias` (optional): The alias of the relation (if it has one)
- `exclude` (optional): A list of columns to exclude
- `prefix` (optional): A string to use as a prefix for all selected column names
- `suffix` (optional): A string to use as a suffix for all selected column names
- `quote_identifiers` (optional): Whether to quote the resulting identifiers, defaults to true

**NOTE**: the `exclude` argument used to be named `except_`. The latter is still supported but we discourage its use because it will be deprecated in the future.

Like all SQLMesh macro functions, omitting an argument when calling `@STAR` requires passing subsequent arguments with their name and the special `:=` keyword operator. For example, we might omit the `alias` argument with `@STAR(foo, exclude := [c])`. Learn more about macro function arguments [below](#positional-and-keyword-arguments).

As a `@STAR` example, consider the following query:

```sql linenums="1"
SELECT
  @STAR(foo, bar, [c], 'baz_', '_qux')
FROM foo AS bar
```

The arguments to `@STAR` are:

1. The name of the table `foo` (from the query's `FROM foo`)
2. The table alias `bar` (from the query's `AS bar`)
3. A list of columns to exclude from the selection, containing one column `c`
4. A string `baz_` to use as a prefix for all column names
5. A string `_qux` to use as a suffix for all column names

`foo` is a table that contains four columns: `a` (`TEXT`), `b` (`TEXT`), `c` (`TEXT`) and `d` (`INT`). After macro expansion, if the column types are known the query would be rendered as:

```sql linenums="1"
SELECT
  CAST("bar"."a" AS TEXT) AS "baz_a_qux",
  CAST("bar"."b" AS TEXT) AS "baz_b_qux",
  CAST("bar"."d" AS INT) AS "baz_d_qux"
FROM foo AS bar
```

Note these aspects of the rendered query:

- Each column is `CAST` to its data type in the table `foo` (e.g., `a` to `TEXT`)
- Each column selection uses the alias `bar` (e.g., `"bar"."a"`)
- Column `c` is not present because it was passed to `@STAR`'s `exclude` argument
- Each column alias is prefixed with `baz_` and suffixed with `_qux` (e.g., `"baz_a_qux"`)

Now consider a more complex example that provides different prefixes to `a` and `b` than to `d` and includes an explicit column `my_column`:

```sql linenums="1"
SELECT
  @STAR(foo, bar, exclude := [c, d], 'ab_pre_'),
  @STAR(foo, bar, exclude := [a, b, c], 'd_pre_'),
  my_column
FROM foo AS bar
```

As before, `foo` is a table that contains four columns: `a` (`TEXT`), `b` (`TEXT`), `c` (`TEXT`) and `d` (`INT`). After macro expansion, the query would be rendered as:

```sql linenums="1"
SELECT
  CAST("bar"."a" AS TEXT) AS "ab_pre_a",
  CAST("bar"."b" AS TEXT) AS "ab_pre_b",
  CAST("bar"."d" AS INT) AS "d_pre_d",
  my_column
FROM foo AS bar
```

Note these aspects of the rendered query:

- Columns `a` and `b` have the prefix `"ab_pre_"` , while column `d` has the prefix `"d_pre_"`
- Column `c` is not present because it was passed to the `exclude` argument in both `@STAR` calls
- `my_column` is present in the query

### @GENERATE_SURROGATE_KEY

`@GENERATE_SURROGATE_KEY` generates a surrogate key from a set of columns. The surrogate key is a sequence of alphanumeric digits returned by a hash function, such as [`MD5`](https://en.wikipedia.org/wiki/MD5), on the concatenated column values.

The surrogate key is created by:
1. `CAST`ing each column's value to `TEXT` (or the SQL engine's equivalent type)
2. Replacing `NULL` values with the text `'_sqlmesh_surrogate_key_null_'` for each column
3. Concatenating the column values after steps (1) and (2)
4. Applying the [`MD5()` hash function](https://en.wikipedia.org/wiki/MD5) to the concatenated value returned by step (3)

For example, the following query:

```sql linenums="1"
SELECT
  @GENERATE_SURROGATE_KEY(a, b, c) AS col
FROM foo
```

would be rendered as:

```sql linenums="1"
SELECT
  MD5(
    CONCAT(
      COALESCE(CAST("a" AS TEXT), '_sqlmesh_surrogate_key_null_'),
      '|',
      COALESCE(CAST("b" AS TEXT), '_sqlmesh_surrogate_key_null_'),
      '|',
      COALESCE(CAST("c" AS TEXT), '_sqlmesh_surrogate_key_null_')
    )
  ) AS "col"
FROM "foo" AS "foo"
```

By default, the `MD5` function is used, but this behavior can change by setting the `hash_function` argument as follows:

```sql linenums="1"
SELECT
  @GENERATE_SURROGATE_KEY(a, b, c, hash_function := 'SHA256') AS col
FROM foo
```

This query will similarly be rendered as:

```sql linenums="1"
SELECT
  SHA256(
    CONCAT(
      COALESCE(CAST("a" AS TEXT), '_sqlmesh_surrogate_key_null_'),
      '|',
      COALESCE(CAST("b" AS TEXT), '_sqlmesh_surrogate_key_null_'),
      '|',
      COALESCE(CAST("c" AS TEXT), '_sqlmesh_surrogate_key_null_')
    )
  ) AS "col"
FROM "foo" AS "foo"
```

### @SAFE_ADD

`@SAFE_ADD` adds two or more operands, substituting `NULL`s with `0`s. It returns `NULL` if all operands are `NULL`.

For example, the following query:

```sql linenums="1"
SELECT
  @SAFE_ADD(a, b, c)
FROM foo
```
would be rendered as:

```sql linenums="1"
SELECT
  CASE WHEN a IS NULL AND b IS NULL AND c IS NULL THEN NULL ELSE COALESCE(a, 0) + COALESCE(b, 0) + COALESCE(c, 0) END
FROM foo
```

### @SAFE_SUB

`@SAFE_SUB` subtracts two or more operands, substituting `NULL`s with `0`s. It returns `NULL` if all operands are `NULL`.

For example, the following query:

```sql linenums="1"
SELECT
  @SAFE_SUB(a, b, c)
FROM foo
```
would be rendered as:

```sql linenums="1"
SELECT
  CASE WHEN a IS NULL AND b IS NULL AND c IS NULL THEN NULL ELSE COALESCE(a, 0) - COALESCE(b, 0) - COALESCE(c, 0) END
FROM foo
```

### @SAFE_DIV

`@SAFE_DIV` divides two numbers, returning `NULL` if the denominator is `0`.

For example, the following query:

```sql linenums="1"
SELECT
  @SAFE_DIV(a, b)
FROM foo
```
would be rendered as:

```sql linenums="1"
SELECT
  a / NULLIF(b, 0)
FROM foo
```

### @UNION

`@UNION` returns a `UNION` query that selects all columns with matching names and data types from the tables.

Its first argument can be either a condition or the `UNION` "type". If the first argument evaluates to a boolean (`TRUE` or `FALSE`), it's treated as a condition. If the condition is `FALSE`, only the first table is returned. If it's `TRUE`, the union operation is performed.

If the first argument is not a boolean condition, it's treated as the `UNION` "type": either `'DISTINCT'` (removing duplicated rows) or `'ALL'` (returning all rows). Subsequent arguments are the tables to be combined.

Let's assume that:

- `foo` is a table that contains three columns: `a` (`INT`), `b` (`TEXT`), `c` (`TEXT`)
- `bar` is a table that contains three columns: `a` (`INT`), `b` (`INT`), `c` (`TEXT`)

Then, the following expression:

```sql linenums="1"
@UNION('distinct', foo, bar)
```

would be rendered as:

```sql linenums="1"
SELECT
  CAST(a AS INT) AS a,
  CAST(c AS TEXT) AS c
FROM foo
UNION
SELECT
  CAST(a AS INT) AS a,
  CAST(c AS TEXT) AS c
FROM bar
```

If the union type is omitted, `'ALL'` is used as the default. So the following expression:

```sql linenums="1"
@UNION(foo, bar)
```

would be rendered as:

```sql linenums="1"
SELECT
  CAST(a AS INT) AS a,
  CAST(c AS TEXT) AS c
FROM foo
UNION ALL
SELECT
  CAST(a AS INT) AS a,
  CAST(c AS TEXT) AS c
FROM bar
```

You can also use a condition to control whether the union happens:

```sql linenums="1"
@UNION(1 > 0, 'all', foo, bar)
```

This would render the same as above. However, if the condition is `FALSE`:

```sql linenums="1"
@UNION(1 > 2, 'all', foo, bar)
```

Only the first table would be selected:

```sql linenums="1"
SELECT
  CAST(a AS INT) AS a,
  CAST(c AS TEXT) AS c
FROM foo
```

### @HAVERSINE_DISTANCE

`@HAVERSINE_DISTANCE` returns the [haversine distance](https://en.wikipedia.org/wiki/Haversine_formula) between two geographic points.

It supports the following arguments, in this order:

- `lat1`: Latitude of the first point
- `lon1`: Longitude of the first point
- `lat2`: Latitude of the second point
- `lon2`: Longitude of the second point
- `unit` (optional): The measurement unit, currently only `'mi'` (miles, default) and `'km'` (kilometers) are supported

SQLMesh macro operators do not accept named arguments. For example, `@HAVERSINE_DISTANCE(lat1=lat_column)` will error.

For example, the following query:

```sql linenums="1"
SELECT
  @HAVERSINE_DISTANCE(driver_y, driver_x, passenger_y, passenger_x, 'mi') AS dist
FROM rides
```

would be rendered as:

```sql linenums="1"
SELECT
  7922 * ASIN(SQRT((POWER(SIN(RADIANS((passenger_y - driver_y) / 2)), 2)) + (COS(RADIANS(driver_y)) * COS(RADIANS(passenger_y)) * POWER(SIN(RADIANS((passenger_x - driver_x) / 2)), 2)))) * 1.0 AS dist
FROM rides
```

### @PIVOT

`@PIVOT` returns a set of columns as a result of pivoting an input column on the specified values. This operation is sometimes described a pivoting from a "long" format (multiple values in a single column) to a "wide" format (one value in each of multiple columns).

It supports the following arguments, in this order:

- `column`: The column to pivot
- `values`: The values to use for pivoting (one column is created for each value in `values`)
- `alias` (optional): Whether to create aliases for the resulting columns, defaults to true
- `agg` (optional): The aggregation function to use, defaults to `SUM`
- `cmp` (optional): The comparison operator to use for comparing the column values, defaults to `=`
- `prefix` (optional): A prefix to use for all aliases
- `suffix` (optional): A suffix to use for all aliases
- `then_value` (optional): The value to be used if the comparison succeeds, defaults to `1`
- `else_value` (optional): The value to be used if the comparison fails, defaults to `0`
- `quote` (optional): Whether to quote the resulting aliases, defaults to true
- `distinct` (optional): Whether to apply a `DISTINCT` clause for the aggregation function, defaults to false

Like all SQLMesh macro functions, omitting an argument when calling `@PIVOT` requires passing subsequent arguments with their name and the special `:=` keyword operator. For example, we might omit the `agg` argument with `@PIVOT(status, ['cancelled', 'completed'], cmp := '<')`. Learn more about macro function arguments [below](#positional-and-keyword-arguments).

For example, the following query:

```sql linenums="1"
SELECT
  date_day,
  @PIVOT(status, ['cancelled', 'completed'])
FROM rides
GROUP BY 1
```

would be rendered as:

```sql linenums="1"
SELECT
  date_day,
  SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS "'cancelled'",
  SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS "'completed'"
FROM rides
GROUP BY 1
```

### @DEDUPLICATE

`@DEDUPLICATE` is used to deduplicate rows in a table based on the specified partition and order columns with a window function.

It supports the following arguments, in this order:

- `relation`: The table or CTE name to deduplicate
- `partition_by`: column names, or expressions to use to identify a window of rows out of which to select one as the deduplicated row
- `order_by`: A list of strings representing the ORDER BY clause, optional - you can add nulls ordering like this: ['<column_name> desc nulls last']

For example, the following query:
```sql linenums="1"
with raw_data as (
@deduplicate(my_table, [id, cast(event_date as date)], ['event_date DESC', 'status ASC'])
)

select * from raw_data
```

would be rendered as:

```sql linenums="1"
WITH "raw_data" AS (
  SELECT
    *
  FROM "my_table" AS "my_table"
  QUALIFY
    ROW_NUMBER() OVER (PARTITION BY "id", CAST("event_date" AS DATE) ORDER BY "event_date" DESC, "status" ASC) = 1
)
SELECT
  *
FROM "raw_data" AS "raw_data"
```

### @DATE_SPINE

`@DATE_SPINE` returns the SQL required to build a date spine. The spine will include the start_date (if it is aligned to the datepart), AND it will include the end_date. This is different from the [`date_spine`](https://github.com/dbt-labs/dbt-utils?tab=readme-ov-file#date_spine-source) macro in `dbt-utils` which will NOT include the end_date. It's typically used to join in unique, hard-coded, date ranges to with other tables/views, so people don't have to constantly adjust date ranges in `where` clauses across many SQL models.

It supports the following arguments, in this order:

- `datepart`: The datepart to use for the date spine - day, week, month, quarter, year
- `start_date`: The start date for the date spine in format YYYY-MM-DD
- `end_date`: The end date for the date spine in format YYYY-MM-DD

For example, the following query:
```sql linenums="1"
WITH discount_promotion_dates AS (
  @date_spine('day', '2024-01-01', '2024-01-16')
)

SELECT * FROM discount_promotion_dates
```

would be rendered as:

```sql linenums="1"
WITH "discount_promotion_dates" AS (
  SELECT
    "_exploded"."date_day" AS "date_day"
  FROM UNNEST(CAST(GENERATE_SERIES(CAST('2024-01-01' AS DATE), CAST('2024-01-16' AS DATE), INTERVAL '1' DAY) AS
DATE[])) AS "_exploded"("date_day")
)
SELECT
  "discount_promotion_dates"."date_day" AS "date_day"
FROM "discount_promotion_dates" AS "discount_promotion_dates"
```

Note: This is DuckDB SQL and other dialects will be transpiled accordingly.
- Recursive CTEs (common table expressions) will be used for `Redshift / MySQL / MSSQL`.
- For `MSSQL` in particular, there's a recursion limit of approximately 100. If this becomes a problem, you can add an `OPTION (MAXRECURSION 0)` clause after the date spine macro logic to remove the limit. This applies for long date ranges.

### @RESOLVE_TEMPLATE

`@resolve_template` is a helper macro intended to be used in situations where you need to gain access to the *components* of the physical object name. It's intended for use in the following situations:

- Providing explicit control over table locations on a per-model basis for engines that decouple storage and compute (such as Athena, Trino, Spark etc)
- Generating references to engine-specific metadata tables that are derived from the physical table name, such as the [`<table>$properties`](https://trino.io/docs/current/connector/iceberg.html#metadata-tables) metadata table in Trino.

Under the hood, it uses the `@this_model` variable so it can only be used during the `creating` and `evaluation` [runtime stages](./macro_variables.md#runtime-variables). Attempting to use it at the `loading` runtime stage will result in a no-op.

The `@resolve_template` macro supports the following arguments:

 - `template` - The string template to render into an AST node
 - `mode` - What type of SQLGlot AST node to return after rendering the template. Valid values are `literal` or `table`. Defaults to `literal`.

The `template` can contain the following placeholders that will be substituted:

  - `@{catalog_name}` - The name of the catalog, eg `datalake`
  - `@{schema_name}` - The name of the physical schema that SQLMesh is using for the model version table, eg `sqlmesh__landing`
  - `@{table_name}` - The name of the physical table that SQLMesh is using for the model version, eg `landing__customers__2517971505`

Note the use of the curly brace syntax `@{}` in the template placeholders - learn more [above](#embedding-variables-in-strings).

The `@resolve_template` macro can be used in a `MODEL` block:

```sql linenums="1" hl_lines="5"
MODEL (
  name datalake.landing.customers,
  ...
  physical_properties (
    location = @resolve_template('s3://warehouse-data/@{catalog_name}/prod/@{schema_name}/@{table_name}')
  )
);
-- CREATE TABLE "datalake"."sqlmesh__landing"."landing__customers__2517971505" ...
-- WITH (location = 's3://warehouse-data/datalake/prod/sqlmesh__landing/landing__customers__2517971505')
```

And also within a query, using `mode := 'table'`:

```sql linenums="1"
SELECT * FROM @resolve_template('@{catalog_name}.@{schema_name}.@{table_name}$properties', mode := 'table')
-- SELECT * FROM "datalake"."sqlmesh__landing"."landing__customers__2517971505$properties"
```

### @AND

`@AND` combines a sequence of operands using the `AND` operator, filtering out any NULL expressions.

For example, the following expression:

```sql linenums="1"
@AND(TRUE, NULL)
```

would be rendered as:

```sql linenums="1"
TRUE
```

### @OR

`@OR` combines a sequence of operands using the `OR` operator, filtering out any NULL expressions.

For example, the following expression:

```sql linenums="1"
@OR(TRUE, NULL)
```

would be rendered as:

```sql linenums="1"
TRUE
```

### SQL clause operators

SQLMesh's macro system has six operators that correspond to different clauses in SQL syntax. They are:

- `@WITH`: common table expression `WITH` clause
- `@JOIN`: table `JOIN` clause(s)
- `@WHERE`: filtering `WHERE` clause
- `@GROUP_BY`: grouping `GROUP BY` clause
- `@HAVING`: group by filtering `HAVING` clause
- `@ORDER_BY`: ordering `ORDER BY` clause
- `@LIMIT`: limiting `LIMIT` clause

Each of these operators is used to dynamically add the code for its corresponding clause to a model's SQL query.

#### How SQL clause operators work

The SQL clause operators take a single argument that determines whether the clause is generated.

If the argument is `TRUE` the clause code is generated, if `FALSE` the code is not. The argument should be written *in SQL* and its value is evaluated with [SQLGlot's](https://github.com/tobymao/sqlglot) SQL engine.

Each SQL clause operator may only be used once in a query, but any common table expressions or subqueries may contain their own single use of the operator as well.

As an example of SQL clause operators, let's revisit the example model from the [User-defined Variables](#user-defined-variables) section above.

As written, the model will always include the `WHERE` clause. We could make its presence dynamic by using the `@WHERE` macro operator:

```sql linenums="1"
MODEL (
  name sqlmesh_example.full_model,
  kind FULL,
  cron '@daily',
  audits (assert_positive_order_ids),
);

@DEF(size, 1);

SELECT
  item_id,
  count(distinct id) AS num_orders,
FROM
  sqlmesh_example.incremental_model
@WHERE(TRUE) item_id > @size
GROUP BY item_id
```

The `@WHERE` argument is set to `TRUE`, so the WHERE code is included in the rendered model:

```sql linenums="1"
SELECT
  item_id,
  count(distinct id) AS num_orders,
FROM
  sqlmesh_example.incremental_model
WHERE item_id > 1
GROUP BY item_id
```

If the `@WHERE` argument were instead set to `FALSE` the `WHERE` clause would be omitted from the query.

These operators aren't too useful if the argument's value is hard-coded. Instead, the argument can consist of code executable by the SQLGlot SQL executor.

For example, the `WHERE` clause will be included in this query because 1 less than 2:

```sql linenums="1"
MODEL (
  name sqlmesh_example.full_model,
  kind FULL,
  cron '@daily',
  audits (assert_positive_order_ids),
);

@DEF(size, 1);

SELECT
  item_id,
  count(distinct id) AS num_orders,
FROM
  sqlmesh_example.incremental_model
@WHERE(1 < 2) item_id > @size
GROUP BY item_id
```

The operator's argument code can include macro variables.

In this example, the two numbers being compared are defined as macro variables instead of being hard-coded:

```sql linenums="1"
MODEL (
  name sqlmesh_example.full_model,
  kind FULL,
  cron '@daily',
  audits (assert_positive_order_ids),
);

@DEF(left_number, 1);
@DEF(right_number, 2);
@DEF(size, 1);

SELECT
  item_id,
  count(distinct id) AS num_orders,
FROM
  sqlmesh_example.incremental_model
@WHERE(@left_number < @right_number) item_id > @size
GROUP BY item_id
```

The argument to `@WHERE` will be "1 < 2" as in the previous hard-coded example after the macro variables `left_number` and `right_number` are substituted in.

### SQL clause operator examples

This section provides brief examples of each SQL clause operator's usage.

The examples use variants of this simple select statement:

```sql linenums="1"
SELECT *
FROM all_cities
```

#### @WITH operator

The `@WITH` operator is used to create [common table expressions](https://en.wikipedia.org/wiki/Hierarchical_and_recursive_queries_in_SQL#Common_table_expression), or "CTEs."

CTEs are typically used in place of derived tables (subqueries in the `FROM` clause) to make SQL code easier to read. Less commonly, recursive CTEs support analysis of hierarchical data with SQL.

```sql linenums="1"
@WITH(True) all_cities as (select * from city)
select *
FROM all_cities
```

renders to

```sql linenums="1"
WITH all_cities as (select * from city)
select *
FROM all_cities
```

#### @JOIN operator

The `@JOIN` operator specifies joins between tables or other SQL objects; it supports different join types (e.g., INNER, OUTER, CROSS, etc.).

```sql linenums="1"
select *
FROM all_cities
LEFT OUTER @JOIN(True) country
  ON city.country = country.name
```

renders to

```sql linenums="1"
select *
FROM all_cities
LEFT OUTER JOIN country
  ON city.country = country.name
```

The `@JOIN` operator recognizes that `LEFT OUTER` is a component of the `JOIN` specification and will omit it if the `@JOIN` argument evaluates to False.

#### @WHERE operator

The `@WHERE` operator adds a filtering `WHERE` clause(s) to the query when its argument evaluates to True.

```sql linenums="1"
SELECT *
FROM all_cities
@WHERE(True) city_name = 'Toronto'
```

renders to

```sql linenums="1"
SELECT *
FROM all_cities
WHERE city_name = 'Toronto'
```

#### @GROUP_BY operator

```sql linenums="1"
SELECT *
FROM all_cities
@GROUP_BY(True) city_id
```

renders to

```sql linenums="1"
SELECT *
FROM all_cities
GROUP BY city_id
```

#### @HAVING operator

```sql linenums="1"
SELECT
count(city_pop) as population
FROM all_cities
GROUP BY city_id
@HAVING(True) population > 1000
```

renders to

```sql linenums="1"
SELECT
count(city_pop) as population
FROM all_cities
GROUP BY city_id
HAVING population > 1000
```

#### @ORDER_BY operator

```sql linenums="1"
SELECT *
FROM all_cities
@ORDER_BY(True) city_pop
```

renders to

```sql linenums="1"
SELECT *
FROM all_cities
ORDER BY city_pop
```

#### @LIMIT operator

```sql linenums="1"
SELECT *
FROM all_cities
@LIMIT(True) 10
```

renders to

```sql linenums="1"
SELECT *
FROM all_cities
LIMIT 10
```

## User-defined macro functions

User-defined macro functions allow the same macro code to be used in multiple models.

SQLMesh supports user-defined macro functions written in two languages - SQL and Python:

- SQL macro functions must use the [Jinja templating system](./jinja_macros.md#user-defined-macro-functions).
- Python macro functions use the SQLGlot library to allow more complex operations than macro variables and operators provide alone.

### Python macro functions

#### Setup

Python macro functions should be placed in `.py` files in the SQLMesh project's `macros` directory. Multiple functions can be defined in one `.py` file, or they can be distributed across multiple files.

An empty `__init__.py` file must be present in the SQLMesh project's `macros` directory. It will be created automatically when the project scaffold is created with `sqlmesh init`.

Each `.py` file containing a macro definition must import SQLMesh's `macro` decorator with `from sqlmesh import macro`.

Python macros are defined as regular python functions adorned with the SQLMesh `@macro()` decorator. The first argument to the function must be `evaluator`, which provides the macro evaluation context in which the macro function will run.

#### Inputs and outputs

Python macros parse all arguments passed to the macro call with SQLGlot before they are used in the function body. Therefore, unless [argument type annotations are provided](#argument-data-types) in the function definition, the macro function code must process SQLGlot expressions and may need to extract the expression's attributes/contents for use.

Python macro functions may return values of either `string` or SQLGlot `expression` types. SQLMesh will automatically parse returned strings into a SQLGlot expression after the function is executed so they can be incorporated into the model query's semantic representation.

Macro functions may [return a list of strings or expressions](#returning-more-than-one-value) that all play the same role in the query (e.g., specifying column definitions). For example, a list containing multiple `CASE WHEN` statements would be incorporated into the query properly, but a list containing both `CASE WHEN` statements and a `WHERE` clause would not.

#### Macro function basics

This example demonstrates the core requirements for defining a python macro - it takes no user-supplied arguments and returns the string `text`.

```python linenums="1"
from sqlmesh import macro

@macro() # Note parentheses at end of `@macro()` decorator
def print_text(evaluator):
  return 'text'
```

We could use this in a SQLMesh SQL model like this:

```sql linenums="1"
SELECT
  @print_text() as my_text
FROM table
```

After processing, it will render to this:

```sql linenums="1"
SELECT
  text as my_text
FROM table
```

Note that the python function returned a string `'text'`, but the rendered query uses `text` as a column name. That is due to the function's returned text being parsed as SQL code by SQLGlot and integrated into the query's semantic representation.

The rendered query will treat `text` as a string if we double-quote the single-quoted value in the function definition as `"'text'"`:

```python linenums="1"
from sqlmesh import macro

@macro()
def print_text(evaluator):
    return "'text'"
```

When run in the same model query as before, this will render to:

```sql linenums="1"
SELECT
  'text' as my_text
FROM table
```

#### Argument data types

Most macro functions provide arguments so users can supply custom values when the function is called. The data type of the argument plays a key role in how the macro code processes its value, and providing type annotations in the macro definition ensures that the macro code receives the data type it expects. This section provides a brief description of SQLMesh macro type annotation - find additional information [below](#typed-macros).

As [mentioned above](#inputs-and-outputs), argument values passed to the macro call are parsed by SQLGlot before they become available to the function code. If an argument does not have a type annotation in the macro function definition, its value will always be a SQLGlot expression in the function body. Therefore, the macro function code must operate directly on the expression (and may need to extract information from it before usage).

If an argument does have a type annotation in the macro function definition, the value passed to the macro call will be coerced to that type after parsing by SQLGlot and before the values are used in the function body. Essentially, SQLMesh will extract the relevant information of the annotated data type from the expression for you (if possible).

For example, this macro function determines whether an argument's value is any of the integers 1, 2, or 3:

```python linenums="1"
from sqlmesh import macro

@macro()
def arg_in_123(evaluator, my_arg):
    return my_arg in [1,2,3]
```

When this macro is called, it will return `FALSE` even if an integer was passed in the call. Consider this macro call:

``` sql linenums="1"
SELECT
  @arg_in_123(1)
```

It returns `SELECT FALSE` because:

1. The passed value `1` is parsed by SQLGlot into a SQLGlot expression before the function code executes and
2. There is no matching SQLGlot expression in `[1,2,3]`

However, the macro will treat the argument like a normal Python function does if we annotate `my_arg` with the integer `int` type in the function definition:

```python linenums="1"
from sqlmesh import macro

@macro()
def arg_in_123(evaluator, my_arg: int): # Type annotation `my_arg: int`
    return my_arg in [1,2,3]
```

Now the macro call will return `SELECT TRUE` because the value is coerced to a Python integer before the function code executes and `1` is in `[1,2,3]`.

If an argument has a default value, the value is not parsed by SQLGlot before the function code executes. Therefore, take care to ensure that the default's data type matches that of a user-supplied argument by adding a type annotation, making the default value a SQLGlot expression, or making the default value `None`.

#### Positional and keyword arguments

In a macro call, the arguments may be provided by position if none are skipped.

For example, consider the `add_args()` function - it has three arguments with default values provided in the function definition:

```python linenums="1"
from sqlmesh import macro

@macro()
def add_args(
    evaluator,
    argument_1: int = 1,
    argument_2: int = 2,
    argument_3: int = 3
):
    return argument_1 + argument_2 + argument_3
```

An `@add_args` call providing values for all arguments accepts positional arguments like this: `@add_args(5, 6, 7)` (which returns 5 + 6 + 7 = `18`). A call omitting and using the default value for the the final `argument_3` can also use positional arguments: `@add_args(5, 6)` (which returns 5 + 6 + 3 = `14`).

However, skipping an argument requires specifying the names of subsequent arguments (i.e., using "keyword arguments"). For example, skipping the second argument above by just omitting it - `@add_args(5, , 7)` - results in an error.

Unlike Python, SQLMesh keyword arguments must use the special operator `:=`. To skip and use the default value for the second argument above, the call must name the third argument: `@add_args(5, argument_3 := 8)` (which returns 5 + 2 + 8 = `15`).

#### Variable-length arguments

The `add_args()` macro defined in the [previous section](#positional-and-keyword-arguments) accepts only three arguments and requires that all three have a value. This greatly limits the macro's flexibility because users may want to add any number of values together.

The macro can be improved by allowing users to provide any number of arguments at call time. We use Python's "variable-length arguments" to accomplish this:

```python linenums="1"
from sqlmesh import macro

@macro()
def add_args(evaluator, *args: int): # Variable-length arguments of integer type `*args: int`
    return sum(args)
```

This macro can be called with one or more arguments. For example:

- `@add_args(1)` returns 1
- `@add_args(1, 2)` returns 3
- `@add_args(1, 2, 3)` returns 6

#### Returning more than one value

Macro functions are a convenient way to tidy model code by creating multiple outputs from one function call. Python macro functions do this by returning a list of strings or SQLGlot expressions.

For example, we might want to create indicator variables from the values in a string column. We can do that by passing in the name of column and a list of values for which it should create indicators, which we then interpolate into `CASE WHEN` statements.

Because SQLMesh parses the input objects, they become SQLGLot expressions in the function body. Therefore, the function code **cannot** treat the input list as a regular Python list.

Two things will happen to the input Python list before the function code is executed:

1. Each of its entries will be parsed by SQLGlot. Different inputs are parsed into different SQLGlot expressions:
    - Numbers are parsed into [`Literal` expressions](https://sqlglot.com/sqlglot/expressions.html#Literal)
    - Quoted strings are parsed into [`Literal` expressions](https://sqlglot.com/sqlglot/expressions.html#Literal)
    - Unquoted strings are parsed into [`Column` expressions](https://sqlglot.com/sqlglot/expressions.html#Column)

2. The parsed entries will be contained in a SQLGlot [`Array` expression](https://sqlglot.com/sqlglot/expressions.html#Array), the SQL entity analogous to a Python list

Because the input  `Array` expression named `values` is not a Python list, we cannot iterate over it directly - instead, we iterate over its `expressions` attribute with `values.expressions`:

```python linenums="1"
from sqlmesh import macro

@macro()
def make_indicators(evaluator, string_column, values):
    cases = []

    for value in values.expressions: # Iterate over `values.expressions`
        cases.append(f"CASE WHEN {string_column} = '{value}' THEN '{value}' ELSE NULL END AS {string_column}_{value}")

    return cases
```

We call this function in a model query to create `CASE WHEN` statements for the `vehicle` column values `truck` and `bus` like this:

```sql linenums="1"
SELECT
  @make_indicators(vehicle, [truck, bus])
FROM table
```

Which renders to:

```sql linenums="1"
SELECT
  CASE WHEN vehicle = 'truck' THEN 'truck' ELSE NULL END AS vehicle_truck,
  CASE WHEN vehicle = 'bus' THEN 'bus' ELSE NULL END AS vehicle_bus,
FROM table
```

Note that in the call `@make_indicators(vehicle, [truck, bus])` none of the three values is quoted.

Because they are unquoted, SQLGlot will parse them all as `Column` expressions. In the places we used single quotes when building the string (`'{value}'`), they will be single-quoted in the output. In the places we did not quote them (`{string_column} = ` and `{string_column}_{value}`), they will not.

#### Accessing predefined and local variable values

[Pre-defined variables](./macro_variables.md#predefined-variables) and [user-defined local variables](#local-variables) can be accessed within the macro's body via the `evaluator.locals` attribute.

The first argument to every macro function, the macro evaluation context `evaluator`, contains macro variable values in its `locals` attribute. `evaluator.locals` is a dictionary whose key:value pairs are macro variables names and the associated values.

For example, a function could access the predefined `execution_epoch` variable containing the epoch timestamp of when the execution started.

```python linenums="1"
from sqlmesh import macro

@macro()
def get_execution_epoch(evaluator):
    return evaluator.locals['execution_epoch']
```

The function would return the `execution_epoch` value when called in a model query:

```sql linenums="1"
SELECT
  @get_execution_epoch() as execution_epoch
FROM table
```

The same approach works for user-defined local macro variables, where the key `"execution_epoch"` would be replaced with the name of the user-defined variable to be accessed.

One downside of that approach to accessing user-defined local variables is that the name of the variable is hard-coded into the function. A more flexible approach is to pass the name of the local macro variable as a function argument:

```python linenums="1"
from sqlmesh import macro

@macro()
def get_macro_var(evaluator, macro_var):
    return evaluator.locals[macro_var]
```

We could define a local macro variable `my_macro_var` with a value of 1 and pass it to the `get_macro_var` function like this:

```sql linenums="1"
MODEL (...);

@DEF(my_macro_var, 1); -- Define local macro variable 'my_macro_var'

SELECT
  @get_macro_var('my_macro_var') as macro_var_value -- Access my_macro_var value from Python macro function
FROM table
```

The model query would render to:

```sql linenums="1"
SELECT
  1 as macro_var_value
FROM table
```

#### Accessing global variable values

[User-defined global variables](#global-variables) can be accessed within the macro's body using the `evaluator.var` method.

If a global variable is not defined, the method will return a Python `None` value. You may provide a different default value as the method's second argument.

For example:

```python linenums="1"
from sqlmesh.core.macros import macro

@macro()
def some_macro(evaluator):
    var_value = evaluator.var("<var_name>") # Default value is `None`
    another_var_value = evaluator.var("<another_var_name>", "default_value") # Default value is `"default_value"`
    ...
```

#### Accessing model, physical table, and virtual layer view names

All SQLMesh models have a name in their `MODEL` specification. We refer to that as the model's "unresolved" name because it may not correspond to any specific object in the SQL engine.

When SQLMesh renders and executes a model, it converts the model name into three forms at different stages:

1. The *fully qualified* name

    - If the model name is of the form `schema.table`, SQLMesh determines the correct catalog and adds it, like `catalog.schema.table`
    - SQLMesh quotes each component of the name using the SQL engine's quoting and case-sensitivity rules, like `"catalog"."schema"."table"`

2. The *resolved* physical table name

    - The qualified name of the model's underlying physical table

3. The *resolved* virtual layer view name

    - The qualified name of the model's virtual layer view in the environment where the model is being executed

You can access any of these three forms in a Python macro through properties of the `evaluation` context object.

Access the unresolved, fully-qualified name through the `this_model_fqn` property.

```python linenums="1"
from sqlmesh.core.macros import macro

@macro()
def some_macro(evaluator):
    # Example:
    # Name in model definition: landing.customers
    # Value returned here: '"datalake"."landing"."customers"'
    unresolved_model_fqn = evaluator.this_model_fqn
    ...
```

Access the resolved physical table and virtual layer view names through the `this_model` property.

The `this_model` property returns different names depending on the runtime stage:

- `promoting` runtime stage: `this_model` resolves to the virtual layer view name

    - Example
        - Model name is `db.test_model`
        - `plan` is running in the `dev` environment
        - `this_model` resolves to `"catalog"."db__dev"."test_model"` (note the `__dev` suffix in the schema name)

- All other runtime stages: `this_model` resolves to the physical table name

    - Example
        - Model name is `db.test_model`
        - `plan` is running in any environment
        - `this_model` resolves to `"catalog"."sqlmesh__project"."project__test_model__684351896"`

```python linenums="1"
from sqlmesh.core.macros import macro

@macro()
def some_macro(evaluator):
    if evaluator.runtime_stage == "promoting":
        # virtual layer view name '"catalog"."db__dev"."test_model"'
        resolved_name = evaluator.this_model
    else:
        # physical table name '"catalog"."sqlmesh__project"."project__test_model__684351896"'
        resolved_name = evaluator.this_model
    ...
```

#### Accessing model schemas

Model schemas can be accessed within a Python macro function through its evaluation context's `column_to_types()` method, if the column types can be statically determined. For instance, a schema of an [external model](../models/external_models.md) can be accessed only after the `sqlmesh create_external_models` command has been executed.

This macro function renames the columns of an upstream model by adding a prefix to them:

```python linenums="1"
from sqlglot import exp
from sqlmesh.core.macros import macro

@macro()
def prefix_columns(evaluator, model_name, prefix: str):
    renamed_projections = []

    # The following converts `model_name`, which is a SQLGlot expression, into a lookup key,
    # assuming that it does not contain quotes. If it did, we would have to generate SQL for
    # each part of `model_name` separately and then concatenate these parts, because in that
    # case `model_name.sql()` would produce an invalid lookup key.
    model_name_sql = model_name.sql()

    for name in evaluator.columns_to_types(model_name_sql):
        new_name = prefix + name
        renamed_projections.append(exp.column(name).as_(new_name))

    return renamed_projections
```

This can then be used in a SQL model like this:

```sql linenums="1"
MODEL (
  name schema.child,
  kind FULL
);

SELECT
  @prefix_columns(schema.parent, 'stg_')
FROM
  schema.parent
```

Note that `columns_to_types` expects an _unquoted model name_, such as `schema.parent`. Since macro arguments without type annotations are SQLGlot expressions, the macro code must extract meaningful information from them. For instance, the lookup key in the above macro definition is extracted by generating the SQL code for `model_name` using the `sql()` method.

Accessing the schema of an upstream model can be useful for various reasons. For example:

- Renaming columns so that downstream consumers are not tightly coupled to external or source tables
- Selecting only a subset of columns that satisfy some criteria (e.g. columns whose names start with a specific prefix)
- Applying transformations to columns, such as masking PII or computing various statistics based on the column types

Thus, leveraging `columns_to_types` can also enable one to write code according to the [DRY](https://en.wikipedia.org/wiki/Don%27t_repeat_yourself) principle, as a single macro function can implement the transformations instead of creating a different macro for each model of interest.

Note: there may be models whose schema is not available when the project is being loaded, in which case a special placeholder column will be returned, aptly named: `__schema_unavailable_at_load__`. In some cases, the macro's implementation will need to account for this placeholder in order to avoid issues due to the schema being unavailable.

#### Accessing snapshots

After a SQLMesh project has been successfully loaded, its snapshots can be accessed in Python macro functions and Python models that generate SQL through the `get_snapshot` method of `MacroEvaluator`.

This enables the inspection of physical table names or the processed intervals for certain snapshots at runtime, as shown in the example below:

```python linenums="1"
from sqlmesh.core.macros import macro

@macro()
def some_macro(evaluator):
    if evaluator.runtime_stage == "evaluating":
        # Check the intervals a snapshot has data for and alter the behavior of the macro accordingly
        intervals = evaluator.get_snapshot("some_model_name").intervals
        ...
    ...
```

#### Using SQLGlot expressions

SQLMesh automatically parses strings returned by Python macro functions into [SQLGlot](https://github.com/tobymao/sqlglot) expressions so they can be incorporated into the model query's semantic representation. Functions can also return SQLGlot expressions directly.

For example, consider a macro function that uses the `BETWEEN` operator in the predicate of a `WHERE` clause. A function returning the predicate as a string might look like this, where the function arguments are substituted into a Python f-string:

```python linenums="1"
from sqlmesh import macro, SQL

@macro()
def between_where(evaluator, column_name: SQL, low_val: SQL, high_val: SQL):
    return f"{column_name} BETWEEN {low_val} AND {high_val}"
```

The function could then be called in a query:

```sql linenums="1"
SELECT
  a
FROM table
WHERE @between_where(a, 1, 3)
```

And it would render to:

```sql linenums="1"
SELECT
  a
FROM table
WHERE a BETWEEN 1 and 3
```

Alternatively, the function could return a [SQLGLot expression](https://github.com/tobymao/sqlglot/blob/main/sqlglot/expressions.py) equivalent to that string by using SQLGlot's expression methods for building semantic representations:

```python linenums="1"
from sqlmesh import macro

@macro()
def between_where(evaluator, column, low_val, high_val):
    return column.between(low_val, high_val)
```

The methods are available because the `column` argument is parsed as a SQLGlot [Column expression](https://sqlglot.com/sqlglot/expressions.html#Column) when the macro function is executed.

Column expressions are sub-classes of the [Condition class](https://sqlglot.com/sqlglot/expressions.html#Condition), so they have builder methods like [`between`](https://sqlglot.com/sqlglot/expressions.html#Condition.between) and [`like`](https://sqlglot.com/sqlglot/expressions.html#Condition.like).

#### Macro pre/post-statements

Macro functions may be used to generate pre/post-statements in a model.

By default, when you first add the pre/post-statement macro functions to a model, SQLMesh will treat those models as directly modified and require a backfill in the next plan. SQLMesh will also treat edits to or removals of pre/post-statement macros as a breaking change.

If your macro does not affect the data returned by a model and you do not want its addition/editing/removal to trigger a backfill, you can specify in the macro definition that it only affects the model's metadata. SQLMesh will still detect changes and create new snapshots for a model when you add/edit/remove the macro, but it will not view the change as breaking and require a backfill.

Specify that a macro only affects a model's metadata by setting the `@macro()` decorator's `metadata_only` argument to `True`. For example:

```python linenums="1" hl_lines="3"
from sqlmesh import macro

@macro(metadata_only=True)
def print_message(evaluator, message):
  print(message)
```

### Typed Macros

Typed macros in SQLMesh bring the power of type hints from Python, enhancing readability, maintainability, and usability of your SQL macros. These macros enable developers to specify expected types for arguments, making the macros more intuitive and less error-prone.

#### Benefits of Typed Macros

1. **Improved Readability**: By specifying types, the intent of the macro is clearer to other developers or future you.
2. **Reduced Boilerplate**: No need for manual type conversion within the macro function, allowing you to focus on the core logic.
3. **Enhanced Autocompletion**: IDEs can provide better autocompletion and documentation based on the specified types.

#### Defining a Typed Macro

Typed macros in SQLMesh use Python's type hints. Here's a simple example of a typed macro that repeats a string a given number of times:

```python linenums="1"
from sqlmesh import macro

@macro()
def repeat_string(evaluator, text: str, count: int):
    return text * count
```

This macro takes two arguments: `text` of type `str` and `count` of type `int`, and it returns a string.

Without type hints, the inputs are two SQLGlot `exp.Literal` objects you would need to manually convert to Python `str` and `int` types. With type hints, you can work with them as string and integer types directly.

Let's try to use the macro in a SQLMesh model:

```sql linenums="1"
SELECT
  @repeat_string('SQLMesh ', 3) as repeated_string
FROM some_table;
```

Unfortunately, this model generates an error when rendered:

```
Error: Invalid expression / Unexpected token. Line 1, Col: 23.
  SQLMesh SQLMesh SQLMesh
```

Why? The macro returned `SQLMesh SQLMesh SQLMesh` as expected, but that string is not valid SQL in the rendered query:

```sql linenums="1" hl_lines="2"
SELECT
  SQLMesh SQLMesh SQLMesh as repeated_string ### invalid SQL code
FROM some_table;
```

The problem is a mismatch between our macro's Python return type `str` and the type expected by the parsed SQL query.

Recall that SQLMesh macros work by modifying the query's semantic representation. In that representation, a SQLGlot string literal type is expected. SQLMesh will do its best to return the type expected by the query's semantic representation, but that is not possible in all scenarios.

Therefore, we must explicitly convert the output with SQLGlot's `exp.Literal.string()` method:

```python linenums="1" hl_lines="5"
from sqlmesh import macro

@macro()
def repeat_string(evaluator, text: str, count: int):
    return exp.Literal.string(text * count)
```

Now the query will render with a valid single-quoted string literal:

```sql linenums="1"
SELECT
  'SQLMesh SQLMesh SQLMesh ' AS "repeated_string"
FROM "some_table" AS "some_table"
```

Typed macros coerce the **inputs** to a macro function, but the macro code is responsible for coercing the **output** to the type expected by the query's semantic representation.

#### Supported Types

SQLMesh supports common Python types for typed macros including:

- `str` -- This handles string literals and basic identifiers, but won't coerce anything more complicated.
- `int`
- `float`
- `bool`
- `datetime.datetime`
- `datetime.date`
- `SQL` -- When you want the SQL string representation of the argument that's passed in
- `list[T]` - where `T` is any supported type including sqlglot expressions
- `tuple[T]` - where `T` is any supported type including sqlglot expressions
- `T1 | T2 | ...` - where `T1`, `T2`, etc. are any supported types including sqlglot expressions

We also support SQLGlot expressions as type hints, allowing you to ensure inputs are coerced to the desired SQL AST node your intending on working with. Some useful examples include:

- `exp.Table`
- `exp.Column`
- `exp.Literal`
- `exp.Identifier`

While these might be obvious examples, you can effectively coerce an input into _any_ SQLGlot expression type, which can be useful for more complex macros. When coercing to more complex types, you will almost certainly need to pass a string literal since expression to expression coercion is limited. When a string literal is passed to a macro that hints at a SQLGlot expression, the string will be parsed using SQLGlot and coerced to the correct type. Failure to coerce to the correct type will result in the original expression being passed to the macro and a warning being logged for the user to address as-needed.

```python linenums="1"
@macro()
def stamped(evaluator, query: exp.Select) -> exp.Subquery:
    return query.select(exp.Literal.string(str(datetime.now())).as_("stamp")).subquery()

# Coercing to a complex node like `exp.Select` works as expected given a string literal input
# SELECT * FROM @stamped('SELECT a, b, c')
```

When coercion fails, there will always be a warning logged but we will not crash. We believe the macro system should be flexible by default, meaning the default behavior is preserved if we cannot coerce. Given that, the user can express whatever level of additional checks they want. For example, if you would like to raise an error when the coercion fails, you can use an `assert` statement. For example:

```python linenums="1"
@macro()
def my_macro(evaluator, table: exp.Table) -> exp.Column:
    assert isinstance(table, exp.Table)
    table.set("catalog", "dev")
    return table

# Works
# SELECT * FROM @my_macro('some.table')
# SELECT * FROM @my_macro(some.table)

# Raises an error thanks to the users inclusion of the assert, otherwise would pass through the string literal and log a warning
# SELECT * FROM @my_macro('SELECT 1 + 1')
```

In using assert this way, you still get the benefits of reducing/removing the boilerplate needed to coerce types; but you **also** get guarantees about the type of the input. This is a useful pattern and is user-defined, so you can use it as you see fit. It ultimately allows you to keep the macro definition clean and focused on the core business logic.

#### Advanced Typed Macros

You can create more complex macros using advanced Python features like generics. For example, a macro that accepts a list of integers and returns their sum:

```python linenums="1"
from typing import List
from sqlmesh import macro

@macro()
def sum_integers(evaluator, numbers: List[int]) -> int:
    return sum(numbers)
```

Usage in SQLMesh:

```sql linenums="1"
SELECT
  @sum_integers([1, 2, 3, 4, 5]) as total
FROM some_table;
```

Generics can be nested and are resolved recursively allowing for fairly robust type hinting.

See examples of the coercion function in action in the test suite [here](https://github.com/SQLMesh/sqlmesh/blob/main/tests/core/test_macros.py).

#### Conclusion

Typed macros in SQLMesh not only enhance the development experience by making macros more readable and easier to use but also contribute to more robust and maintainable code. By leveraging Python's type hinting system, developers can create powerful and intuitive macros for their SQL queries, further bridging the gap between SQL and Python.

## Mixing macro systems

SQLMesh supports both SQLMesh and [Jinja](./jinja_macros.md) macro systems. We strongly recommend using only one system in a model - if both are present, they may fail or behave in unintuitive ways.
