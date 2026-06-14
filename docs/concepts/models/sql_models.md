# SQL models

SQL models are the main type of models used by SQLMesh. These models can be defined using either SQL or Python that generates SQL.

## SQL-based definition

The SQL-based definition of SQL models is the most common one, and consists of the following sections:

* The `MODEL` DDL
* Optional pre-statements
* A single query
* Optional post-statements
* Optional on-virtual-update-statements

These models are designed to look and feel like you're simply using SQL, but they can be customized for advanced use cases.

To create a SQL-based model, add a new file with the `.sql` suffix into the `models/` directory (or a subdirectory of `models/`) within your SQLMesh project. Although the name of the file doesn't matter, it is customary to use the model's name (without the schema) as the file name. For example, the file containing the model `sqlmesh_example.seed_model` would be named `seed_model.sql`.

### Example

```sql linenums="1"
-- This is the MODEL DDL, where you specify model metadata and configuration information.
MODEL (
  name db.customers,
  kind FULL,
);

/*
  Optional pre-statements that will run before the model's query.
  You should NOT do things that cause side effects that could error out when
  executed concurrently with other statements, such as creating physical tables.
*/
CACHE TABLE countries AS SELECT * FROM raw.countries;

/*
  This is the single query that defines the model's logic.
  Although it is not required, it is considered best practice to explicitly
  specify the type for each one of the model's columns through casting.
*/
SELECT
  r.id::INT,
  r.name::TEXT,
  c.country::TEXT
FROM raw.restaurants AS r
JOIN countries AS c
  ON r.id = c.restaurant_id;

/*
  Optional post-statements that will run after the model's query.
  You should NOT do things that cause side effects that could error out when
  executed concurrently with other statements, such as creating physical tables.
*/
UNCACHE TABLE countries;
```

### `MODEL` DDL

The `MODEL` DDL is used to specify metadata about the model such as its name, [kind](./model_kinds.md), owner, cron, and others. This should be the first statement in your SQL-based model's file.

Refer to `MODEL` [properties](./overview.md#properties) for the full list of allowed properties.

### Optional pre/post-statements

Optional pre/post-statements allow you to execute SQL commands before and after a model runs, respectively.

For example, pre/post-statements might modify settings or create a table index. However, be careful not to run any statement that could conflict with the execution of another model if they are run concurrently, such as creating a physical table.

Pre/post-statements are just standard SQL commands located before/after the model query. They must end with a semi-colon, and the model query must end with a semi-colon if a post-statement is present. The [example above](#example) contains both pre- and post-statements.

**Project-level defaults:** You can also define pre/post-statements at the project level using `model_defaults` in your configuration. These will be applied to all models in your project and merged with any model-specific statements. Default statements are executed first, followed by model-specific statements. Learn more about this in the [model configuration reference](../../reference/model_configuration.md#model-defaults).

!!! warning

    Pre/post-statements are evaluated twice: when a model's table is created and when its query logic is evaluated. Executing statements more than once can have unintended side-effects, so you can [conditionally execute](../macros/sqlmesh_macros.md#prepost-statements) them based on SQLMesh's [runtime stage](../macros/macro_variables.md#runtime-variables).

The pre/post-statements in the [example above](#example) will run twice because they are not conditioned on runtime stage.

We can condition the post-statement to only run after the model query is evaluated using the [`@IF` macro operator](../macros/sqlmesh_macros.md#if) and [`@runtime_stage` macro variable](../macros/macro_variables.md#runtime-variables) like this:

```sql linenums="1" hl_lines="8-11"
MODEL (
  name db.customers,
  kind FULL,
);

[...same as example above...]

@IF(
  @runtime_stage = 'evaluating',
  UNCACHE TABLE countries
);
```

Note that the SQL command `UNCACHE TABLE countries` inside the `@IF()` macro does **not** end with a semi-colon. Instead, the semi-colon comes after the `@IF()` macro's closing parenthesis.

### Optional on-virtual-update statements

The optional on-virtual-update statements allow you to execute SQL commands after the completion of the [Virtual Update](#virtual-update).

These can be used, for example, to grant privileges on views of the virtual layer.

**Project-level defaults:** You can also define on-virtual-update statements at the project level using `model_defaults` in your configuration. These will be applied to all models in your project and merged with any model-specific statements. Default statements are executed first, followed by model-specific statements. Learn more about this in the [model configuration reference](../../reference/model_configuration.md#model-defaults).

These SQL statements must be enclosed within an `ON_VIRTUAL_UPDATE_BEGIN;` ...; `ON_VIRTUAL_UPDATE_END;` block like this:

```sql linenums="1" hl_lines="10-15"
MODEL (
  name db.customers,
  kind FULL
);

SELECT
  r.id::INT
FROM raw.restaurants AS r;

ON_VIRTUAL_UPDATE_BEGIN;
GRANT SELECT ON VIEW @this_model TO ROLE role_name;
JINJA_STATEMENT_BEGIN;
GRANT SELECT ON VIEW {{ this_model }} TO ROLE admin;
JINJA_END;
ON_VIRTUAL_UPDATE_END;
```

[Jinja expressions](../macros/jinja_macros.md) can also be used within them, as demonstrated in the example above. These expressions must be properly nested within a `JINJA_STATEMENT_BEGIN;` and `JINJA_END;` block.

!!! note

    Table resolution for these statements occurs at the virtual layer. This means that table names, including `@this_model` macro, are resolved to their qualified view names. For instance, when running the plan in an environment named `dev`, `db.customers` and `@this_model` would resolve to `db__dev.customers` and not to the physical table name.

### The model query

The model must contain a standalone query, which can be a single `SELECT` expression, or multiple `SELECT` expressions combined with the `UNION`, `INTERSECT`, or `EXCEPT` operators. The result of this query will be used to populate the model's table or view.

### SQL model blueprinting

A SQL model can also serve as a template for creating multiple models, or _blueprints_, by specifying a list of key-value mappings in the `blueprints` property. In order to achieve this, the model's name must be parameterized with a variable that exists in this mapping.

For instance, the following model will result into two new models, each using the corresponding mapping in the `blueprints` property:

```sql linenums="1"
MODEL (
  name @customer.some_table,
  kind FULL,
  blueprints (
    (customer := customer1, field_a := x, field_b := y),
    (customer := customer2, field_a := z, field_b := w)
  )
);

SELECT
  @field_a,
  @{field_b} AS field_b,
  @'prefix_@{field_a}_suffix' AS literal_example
FROM @customer.some_source
```

The two models produced from this template are:

```sql linenums="1"
-- This uses the first variable mapping
MODEL (
  name customer1.some_table,
  kind FULL
);

SELECT
  x,
  y AS field_b,
  'prefix_x_suffix' AS literal_example
FROM customer1.some_source

-- This uses the second variable mapping
MODEL (
  name customer2.some_table,
  kind FULL
);

SELECT
  z,
  w AS field_b,
  'prefix_z_suffix' AS literal_example
FROM customer2.some_source
```

Both `@field_a` and `@{field_b}` resolve blueprint variable values as SQL identifiers. The curly brace syntax is useful when embedding a variable within a larger string where the variable boundary would otherwise be ambiguous (e.g. `@{customer}_suffix`). To produce a string literal with interpolated variables, use the `@'...@{var}...'` syntax as shown with `literal_example` above. Learn more about the curly brace syntax [here](../../concepts/macros/sqlmesh_macros.md#embedding-variables-in-strings).

Blueprint variable mappings can also be constructed dynamically, e.g., by using a macro: `blueprints @gen_blueprints()`. This is useful in cases where the `blueprints` list needs to be sourced from external sources, such as CSV files.

For example, the definition of the `gen_blueprints` may look like this:

```python linenums="1"
from sqlmesh import macro

@macro()
def gen_blueprints(evaluator):
    return (
        "((customer := customer1, field_a := x, field_b := y),"
        " (customer := customer2, field_a := z, field_b := w))"
    )
```

It's also possible to use the `@EACH` macro, combined with a global list variable (`@values`):

```sql linenums="1"
MODEL (
  name @customer.some_table,
  kind FULL,
  blueprints @EACH(@values, x -> (customer := schema_@x)),
);

SELECT
  1 AS c
```

## Python-based definition

The Python-based definition of SQL models consists of a single python function, decorated with SQLMesh's `@model` [decorator](https://wiki.python.org/moin/PythonDecorators). The decorator is required to have the `is_sql` keyword argument set to `True` to distinguish it from [Python models](./python_models.md) that return DataFrame instances.

This function's return value serves as the model's query, and it must be either a SQL string or a [SQLGlot expression](https://github.com/tobymao/sqlglot/blob/main/sqlglot/expressions.py). The `@model` decorator is used to define the model's [metadata](#MODEL-DDL) and, optionally its pre/post-statements or on-virtual-update-statements that are also in the form of SQL strings or SQLGlot expressions.

Defining a SQL model using Python can be beneficial in cases where its query is too complex to express cleanly in SQL, for example due to having many dynamic components that would require heavy use of [macros](../macros/overview/). Since Python-based models generate SQL, they support the same features as regular SQL models, such as column-level [lineage](../glossary/#lineage).

To create a Python-based model, add a new file with the `.py` suffix into the `models/` directory (or a subdirectory of `models/`) within your SQLMesh project. The file naming conventions of Python-based models are similar to those of SQL-based models. Inside this file, define a function named `entrypoint` with a single `evaluator` argument, as shown in the example below.

### Example

The following example demonstrates how the above `db.customers` model can be defined as a Python-based model using SQLGlot's `Expression` builder methods:

```python linenums="1"
from sqlglot import exp

from sqlmesh.core.model import model
from sqlmesh.core.macros import MacroEvaluator

@model(
    "db.customers",
    is_sql=True,
    kind="FULL",
    pre_statements=["CACHE TABLE countries AS SELECT * FROM raw.countries"],
    post_statements=["UNCACHE TABLE countries"],
    on_virtual_update=["GRANT SELECT ON VIEW @this_model TO ROLE dev_role"],
)
def entrypoint(evaluator: MacroEvaluator) -> str | exp.Expression:
    return (
        exp.select("r.id::int", "r.name::text", "c.country::text")
        .from_("raw.restaurants as r")
        .join("countries as c", on="r.id = c.restaurant_id")
    )
```

One could also define this model by simply returning a string that contained the SQL query of the SQL-based example. Strings used as pre/post-statements or return values in Python-based models will be parsed into SQLGlot expressions, which means that SQLMesh will still be able to understand them semantically and thus provide information such as column-level lineage.

!!! note

    Since python models have access to the macro evaluation context (`MacroEvaluator`), they can also [access model schemas](../macros/sqlmesh_macros.md#accessing-model-schemas) through its `columns_to_types` method.

### `@model` decorator

The `@model` decorator is the Python equivalent of the `MODEL` DDL.

In addition to model metadata and configuration information, one can also set the keyword arguments `pre_statements`, `post_statements` and `on_virtual_update` to a list of SQL strings and/or SQLGlot expressions to define the pre/post-statements and on-virtual-update-statements of the model, respectively.

!!! note

    All of the [metadata property](./overview.md#model-properties) field names are the same as those in the `MODEL` DDL.

### Python model blueprinting

A Python-based SQL model can also serve as a template for creating multiple models, or _blueprints_, by specifying a list of key-value dicts in the `blueprints` property. In order to achieve this, the model's name must be parameterized with a variable that exists in this mapping.

For instance, the following model will result into two new models, each using the corresponding mapping in the `blueprints` property:

```python linenums="1"
from sqlglot import exp

from sqlmesh.core.model import model
from sqlmesh.core.macros import MacroEvaluator

@model(
    "@{customer}.some_table",
    is_sql=True,
    kind="FULL",
    blueprints=[
        {"customer": "customer1", "field_a": "x", "field_b": "y"},
        {"customer": "customer2", "field_a": "z", "field_b": "w"},
    ],
)
def entrypoint(evaluator: MacroEvaluator) -> str | exp.Expression:
    field_a = evaluator.blueprint_var("field_a")
    field_b = evaluator.blueprint_var("field_b")
    customer = evaluator.blueprint_var("customer")

    return exp.select(field_a, field_b).from_(f"{customer}.some_source")
```

The two models produced from this template are the same as in the [example](#SQL-model-blueprinting) for SQL-based blueprinting.

Blueprint variable mappings can also be constructed dynamically, e.g., by using a macro: `blueprints="@gen_blueprints()"`. This is useful in cases where the `blueprints` list needs to be sourced from external sources, such as CSV files.

For example, the definition of the `gen_blueprints` may look like this:

```python linenums="1"
from sqlmesh import macro

@macro()
def gen_blueprints(evaluator):
    return (
        "((customer := customer1, field_a := x, field_b := y),"
        " (customer := customer2, field_a := z, field_b := w))"
    )
```

It's also possible to use the `@EACH` macro, combined with a global list variable (`@values`):

```python linenums="1"

@model(
    "@{customer}.some_table",
    is_sql=True,
    blueprints="@EACH(@values, x -> (customer := schema_@x))",
    ...
)
...
```

## Automatic dependencies

SQLMesh parses your SQL, so it understands what the code does and how it relates to other models. There is no need for you to manually specify dependencies to other models with special tags or commands.

For example, consider a model with this query:

```sql linenums="1"
SELECT employees.id
FROM employees
JOIN countries
  ON employees.id = countries.employee_id
```

SQLMesh will detect that the model depends on both `employees` and `countries`. When executing this model, it will ensure that `employees` and `countries` are executed first.

External dependencies not defined in SQLMesh are also supported. SQLMesh can either depend on them implicitly through the order in which they are executed, or through [signals](../../guides/signals.md).

Although automatic dependency detection works most of the time, there may be specific cases for which you want to define dependencies manually. You can do so in the `MODEL` DDL with the [dependencies property](./overview.md#properties).

## Conventions

SQLMesh encourages explicitly specifying the data types of a model's columns through casting. This allows SQLMesh to understand the data types in your models, and it prevents incorrect type inference. SQLMesh supports the casting format `<column name>::<data type>` in models of any SQL dialect.

### Explicit SELECTs

Although `SELECT *` is convenient, it is dangerous because a model's results can change due to external factors (e.g., an upstream source adding or removing a column). In general, we encourage listing out every column you need or using [`create_external_models`](../../reference/cli.md#create_external_models) to capture the schema of an external data source.

If you select from an external source, `SELECT *` will prevent SQLMesh from performing some optimization steps and from determining upstream column-level lineage. Use an [`external` model kind](./model_kinds.md#external) to enable optimizations and upstream column-level lineage for external sources.

### Encoding

SQLMesh expects files containing SQL models to be encoded according to the [UTF-8](https://en.wikipedia.org/wiki/UTF-8) standard. Using a different encoding may lead to unexpected behavior.

## Transpilation

SQLMesh leverages [SQLGlot](https://github.com/tobymao/sqlglot) to parse and transpile SQL. Therefore, you can write your SQL in any supported dialect and transpile it into another supported dialect.

You can also use advanced syntax that may not be available in your engine of choice. For example, `x::int` is equivalent to `CAST(x as INT)`, but is only supported in some dialects. SQLGlot allows you to use this feature regardless of what engine you're using.

Additionally, you won't have to worry about minor formatting differences such as trailing commas, as SQLGlot will remove them at parse time.

## Macros

Although standard SQL is very powerful, complex data systems often require running SQL queries with dynamic components such as date filters. For example, you may want to change the date ranges in a `between` statement so that you can get the latest batch of data. SQLMesh provides these dates automatically through [macro variables](../macros/macro_variables.md).

Additionally, large queries can be difficult to read and maintain. In order to make queries more compact, SQLMesh supports a powerful [macro syntax](../macros/overview.md) as well as [Jinja](https://jinja.palletsprojects.com/en/3.1.x/), allowing you to write macros that make your SQL queries easier to manage.
