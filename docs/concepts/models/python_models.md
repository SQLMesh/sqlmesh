# Python models

Although SQL is a powerful tool, some use cases are better handled by Python. For example, Python may be a better option in pipelines that involve machine learning, interacting with external APIs, or complex business logic that cannot be expressed in SQL.

SQLMesh has first-class support for models defined in Python; there are no restrictions on what can be done in the Python model as long as it returns a Pandas or Spark DataFrame instance.


!!! info "Unsupported model kinds"

    Python models do not support these [model kinds](./model_kinds.md) - use a SQL model instead.

    * `VIEW`
    * `SEED`
    * `MANAGED`
    * `EMBEDDED`

## Definition

To create a Python model, add a new file with the `*.py` extension to the `models/` directory. Inside the file, define a function named `execute`. For example:

```python linenums="1"
import typing as t
from datetime import datetime

from sqlmesh import ExecutionContext, model

@model(
    "my_model.name",
    columns={
        "column_name": "int",
    },
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:
```

The `execute` function is wrapped with the `@model` [decorator](https://wiki.python.org/moin/PythonDecorators), which is used to capture the model's metadata (similar to the `MODEL` DDL statement in [SQL models](./sql_models.md)).

Because SQLMesh creates tables before evaluating models, the schema of the output DataFrame is a required argument. The `@model` argument `columns` contains a dictionary of column names to types.

The function takes an `ExecutionContext` that is able to run queries and to retrieve the current time interval that is being processed, along with arbitrary key-value arguments passed in at runtime. The function can either return a Pandas, PySpark, Bigframe, or Snowpark Dataframe instance.

If the function output is too large, it can also be returned in chunks using Python generators.

## `@model` specification

The arguments provided in the `@model` specification have the same names as those provided in a SQL model's `MODEL` DDL.

Python model `kind`s are specified with a Python dictionary containing the kind's name and arguments. All model kind arguments are listed in the [models configuration reference page](../../reference/model_configuration.md#model-kind-properties).

The model `kind` dictionary must contain a `name` key whose value is a member of the [`ModelKindName` enum class](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName). The `ModelKindName` class must be imported at the beginning of the model definition file before being used in the `@model` specification.

Supported `kind` dictionary `name` values are:

- `ModelKindName.VIEW`
- `ModelKindName.FULL`
- `ModelKindName.SEED`
- `ModelKindName.INCREMENTAL_BY_TIME_RANGE`
- `ModelKindName.INCREMENTAL_BY_UNIQUE_KEY`
- `ModelKindName.INCREMENTAL_BY_PARTITION`
- `ModelKindName.SCD_TYPE_2_BY_TIME`
- `ModelKindName.SCD_TYPE_2_BY_COLUMN`
- `ModelKindName.EMBEDDED`
- `ModelKindName.CUSTOM`
- `ModelKindName.MANAGED`
- `ModelKindName.EXTERNAL`

This example demonstrates how to specify an incremental by time range model kind in Python:

```python linenums="1"
from sqlmesh import ExecutionContext, model
from sqlmesh.core.model.kind import ModelKindName

@model(
    "docs_example.incremental_model",
    kind=dict(
        name=ModelKindName.INCREMENTAL_BY_TIME_RANGE,
        time_column="model_time_column"
    )
)
```

## Execution context
Python models can do anything you want, but it is strongly recommended for all models to be [idempotent](../glossary.md#idempotency). Python models can fetch data from upstream models or even data outside of SQLMesh.

Given an execution `ExecutionContext` "context", you can fetch a DataFrame with the `fetchdf` method:

```python linenums="1"
df = context.fetchdf("SELECT * FROM my_table")
```

## Optional pre/post-statements

Optional pre/post-statements allow you to execute SQL commands before and after a model runs, respectively.

For example, pre/post-statements might modify settings or create indexes. However, be careful not to run any statement that could conflict with the execution of another statement if models run concurrently, such as creating a physical table.

You can set the `pre_statements` and `post_statements` arguments to a list of SQL strings, SQLGlot expressions, or macro calls to define the model's pre/post-statements.

**Project-level defaults:** You can also define pre/post-statements at the project level using `model_defaults` in your configuration. These will be applied to all models in your project and merged with any model-specific statements. Default statements are executed first, followed by model-specific statements. Learn more about this in the [model configuration reference](../../reference/model_configuration.md#model-defaults).

``` python linenums="1" hl_lines="8-12"
@model(
    "db.test_model",
    kind="full",
    columns={
        "id": "int",
        "name": "text",
    },
    pre_statements=[
        "SET GLOBAL parameter = 'value';",
        exp.Cache(this=exp.table_("x"), expression=exp.select("1")),
    ],
    post_statements=["@CREATE_INDEX(@this_model, id)"],
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:

    return pd.DataFrame([
        {"id": 1, "name": "name"}
    ])

```

The previous example's `post_statements` called user-defined SQLMesh macro `@CREATE_INDEX(@this_model, id)`.

We could define the `CREATE_INDEX` macro in the project's `macros` directory like this. The macro creates a table index on a single column, conditional on the [runtime stage](../macros/macro_variables.md#runtime-variables) being `creating` (table creation time).


``` python linenums="1"
@macro()
def create_index(
    evaluator: MacroEvaluator,
    model_name: str,
    column: str,
):
    if evaluator.runtime_stage == "creating":
        return f"CREATE INDEX idx ON {model_name}({column});"
    return None
```

Alternatively, pre- and post-statements can be issued with the SQLMesh [`fetchdf` method](../../reference/cli.md#fetchdf) [described above](#execution-context).

Pre-statements may be specified anywhere in the function body before it `return`s or `yield`s. Post-statements must execute after the function completes, so instead of `return`ing a value the function must `yield` the value. The post-statement must be specified after the `yield`.

This example function includes both pre- and post-statements:

``` python linenums="1" hl_lines="9-10 12 17-18"
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:

    # pre-statement
    context.engine_adapter.execute("SET GLOBAL parameter = 'value';")

    # post-statement requires using `yield` instead of `return`
    yield pd.DataFrame([
        {"id": 1, "name": "name"}
    ])

    # post-statement
    context.engine_adapter.execute("CREATE INDEX idx ON example.pre_post_statements (id);")
```

## Optional on-virtual-update statements

The optional on-virtual-update statements allow you to execute SQL commands after the completion of the [Virtual Update](#virtual-update).

These can be used, for example, to grant privileges on views of the virtual layer.

Similar to pre/post-statements you can set the `on_virtual_update` argument in the `@model` decorator to a list of SQL strings, SQLGlot expressions, or macro calls.

**Project-level defaults:** You can also define on-virtual-update statements at the project level using `model_defaults` in your configuration. These will be applied to all models in your project (including Python models) and merged with any model-specific statements. Default statements are executed first, followed by model-specific statements. Learn more about this in the [model configuration reference](../../reference/model_configuration.md#model-defaults).

``` python linenums="1" hl_lines="8"
@model(
    "db.test_model",
    kind="full",
    columns={
        "id": "int",
        "name": "text",
    },
    on_virtual_update=["GRANT SELECT ON VIEW @this_model TO ROLE dev_role"],
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:

    return pd.DataFrame([
        {"id": 1, "name": "name"}
    ])
```

!!! note

    Table resolution for these statements occurs at the virtual layer. This means that table names, including `@this_model` macro, are resolved to their qualified view names. For instance, when running the plan in an environment named `dev`, `db.test_model` and `@this_model` would resolve to `db__dev.test_model` and not to the physical table name.

## Dependencies

In order to fetch data from an upstream model, you first get the table name using `context`'s `resolve_table` method. This returns the appropriate table name for the current runtime [environment](../environments.md):

```python linenums="1"
table = context.resolve_table("docs_example.upstream_model")
df = context.fetchdf(f"SELECT * FROM {table}")
```

The `resolve_table` method will automatically add the referenced model to the Python model's dependencies.

The only other way to set dependencies of models in Python models is to define them explicitly in the `@model` decorator using the keyword `depends_on`. The dependencies defined in the model decorator take precedence over any dynamic references inside the function.

In this example, only `upstream_dependency` will be captured, while `another_dependency` will be ignored:

```python linenums="1"
@model(
    "my_model.with_explicit_dependencies",
    depends_on=["docs_example.upstream_dependency"], # captured
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:

    # ignored due to @model dependency "upstream_dependency"
    context.resolve_table("docs_example.another_dependency")
```

User-defined [global variables](global-variables) or [blueprint variables](#python-model-blueprinting) can also be used in `resolve_table` calls, as shown in the following example (similarly for `blueprint_var()`):

```python linenums="1"
@model(
    "@schema_name.test_model2",
    kind="FULL",
    columns={"id": "INT"},
)
def execute(context, **kwargs):
    table = context.resolve_table(f"{context.var('schema_name')}.test_model1")
    select_query = exp.select("*").from_(table)
    return context.fetchdf(select_query)
```

## Returning empty dataframes

Python models may not return an empty dataframe.

If your model could possibly return an empty dataframe, conditionally `yield` the dataframe or an empty generator instead of `return`ing:

```python linenums="1" hl_lines="10-13"
@model(
    "my_model.empty_df"
)
def execute(
    context: ExecutionContext,
) -> pd.DataFrame:

    [...code creating df...]

    if df.empty:
        yield from ()
    else:
        yield df
```

## User-defined variables

[User-defined global variables](../../reference/configuration.md#variables) can be accessed from within the Python model with the `context.var` method.

For example, this model access the user-defined variables `var` and `var_with_default`. It specifies a default value of `default_value` if `variable_with_default` resolves to a missing value.

```python linenums="1" hl_lines="11 12"
@model(
    "my_model.name",
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:
    var_value = context.var("var")
    var_with_default_value = context.var("var_with_default", "default_value")
    ...
```

Alternatively, you can access global variables via `execute` function arguments, where the name of the argument corresponds to the name of a variable key.

For example, this model specifies `my_var` as an argument to the `execute` method. The model code can reference the `my_var` object directly:

```python linenums="1" hl_lines="9 12"
@model(
    "my_model.name",
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    my_var: Optional[str] = None,
    **kwargs: t.Any,
) -> pd.DataFrame:
    my_var_plus1 = my_var + 1
    ...
```

Make sure the argument has a default value if it's possible for the variable to be missing.

Note that arguments must be specified explicitly - variables cannot be accessed using `kwargs`.

## Python model blueprinting

A Python model can also serve as a template for creating multiple models, or _blueprints_, by specifying a list of key-value dicts in the `blueprints` property. In order to achieve this, the model's name must be parameterized with a variable that exists in this mapping.

For instance, the following model will result into two new models, each using the corresponding mapping in the `blueprints` property:

```python linenums="1"
import typing as t
from datetime import datetime

import pandas as pd
from sqlmesh import ExecutionContext, model

@model(
    "@{customer}.some_table",
    kind="FULL",
    blueprints=[
        {"customer": "customer1", "field_a": "x", "field_b": "y"},
        {"customer": "customer2", "field_a": "z", "field_b": "w"},
    ],
    columns={
        "field_a": "text",
        "field_b": "text",
        "customer": "text",
    },
)
def entrypoint(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "field_a": [context.blueprint_var("field_a")],
            "field_b": [context.blueprint_var("field_b")],
            "customer": [context.blueprint_var("customer")],
        }
    )
```

Blueprint variables can also be used as **column names and column types** in the `columns` dictionary. For example, if each blueprint produces a model with a different set of column names and types, both can be parameterized using the same `@{variable}` syntax:

```python linenums="1"
import pandas as pd
from sqlmesh import ExecutionContext, model

@model(
    "@{customer}.metrics",
    kind="FULL",
    blueprints=[
        {"customer": "customer1", "primary_metric": "revenue", "primary_type": "int",  "secondary_metric": "cost",   "secondary_type": "double"},
        {"customer": "customer2", "primary_metric": "sales",   "primary_type": "text", "secondary_metric": "profit", "secondary_type": "double"},
    ],
    columns={
        "@{primary_metric}": "@{primary_type}",
        "@{secondary_metric}": "@{secondary_type}",
    },
)
def entrypoint(context: ExecutionContext, **kwargs) -> pd.DataFrame:
    return pd.DataFrame({
        context.blueprint_var("primary_metric"): [1],
        context.blueprint_var("secondary_metric"): [1.5],
    })
```

Global variables (defined in the project config) can also be used as column names and types in the same way.

Note the use of curly brace syntax `@{customer}` in the model name above. It is used to ensure SQLMesh can combine the macro variable into the model name identifier correctly - learn more [here](../../concepts/macros/sqlmesh_macros.md#embedding-variables-in-strings).

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
    blueprints="@EACH(@values, x -> (customer := schema_@x))",
    ...
)
...
```

## Using macros in model properties

Python models support macro variables in model properties. However, special care must be taken when the macro variable appears within a string.

For example when using macro variables inside cron expressions, you need to wrap the entire expression in quotes and prefix it with `@` to ensure proper parsing:

```python linenums="1"
# Correct: Wrap the cron expression containing a macro variable
@model(
    "my_model",
    cron="@'*/@{mins} * * * *'",  # Note the @'...' syntax
    ...
)

# This also works with blueprint variables
@model(
    "@{customer}.scheduled_model",
    cron="@'0 @{hour} * * *'",
    blueprints=[
        {"customer": "customer_1", "hour": 2}, # Runs at 2 AM
        {"customer": "customer_2", "hour": 8}, # Runs at 8 AM
    ],
    ...
)

```

This is necessary because cron expressions often use `@` for aliases (like `@daily`, `@hourly`), which can conflict with SQLMesh's macro syntax.

## Examples
### Basic
The following is an example of a Python model returning a static Pandas DataFrame.

**Note:** All of the [metadata](./overview.md#properties) field names are the same as those in the SQL `MODEL` DDL.

```python linenums="1"
import typing as t
from datetime import datetime

import pandas as pd
from sqlglot.expressions import to_column
from sqlmesh import ExecutionContext, model

@model(
    "docs_example.basic",
    owner="janet",
    cron="@daily",
    columns={
        "id": "int",
        "name": "text",
    },
    column_descriptions={
        "id": "Unique ID",
        "name": "Name corresponding to the ID",
    },
    audits=[
        ("not_null", {"columns": [to_column("id")]}),
    ],
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:

    return pd.DataFrame([
        {"id": 1, "name": "name"}
    ])
```

### SQL Query and Pandas
The following is a more complex example that queries an upstream model and outputs a Pandas DataFrame:

```python linenums="1"
import typing as t
from datetime import datetime

import pandas as pd
from sqlmesh import ExecutionContext, model

@model(
    "docs_example.sql_pandas",
    columns={
        "id": "int",
        "name": "text",
    },
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:
    # get the upstream model's name and register it as a dependency
    table = context.resolve_table("upstream_model")

    # fetch data from the model as a pandas DataFrame
    # if the engine is spark, this returns a spark DataFrame
    df = context.fetchdf(f"SELECT id, name FROM {table}")

    # do some pandas stuff
    df[id] += 1
    return df
```

### PySpark
This example demonstrates using the PySpark DataFrame API. If you use Spark, the DataFrame API is preferred to Pandas since it allows you to compute in a distributed fashion.

```python linenums="1"
import typing as t
from datetime import datetime

import pandas as pd
from pyspark.sql import DataFrame, functions

from sqlmesh import ExecutionContext, model

@model(
    "docs_example.pyspark",
    columns={
        "id": "int",
        "name": "text",
        "country": "text",
    },
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> DataFrame:
    # get the upstream model's name and register it as a dependency
    table = context.resolve_table("upstream_model")

    # use the spark DataFrame api to add the country column
    df = context.spark.table(table).withColumn("country", functions.lit("USA"))

    # returns the pyspark DataFrame directly, so no data is computed locally
    return df
```


### Snowpark
This example demonstrates using the Snowpark DataFrame API. If you use Snowflake, the DataFrame API is preferred to Pandas since it allows you to compute in a distributed fashion.

```python linenums="1"
import typing as t
from datetime import datetime

import pandas as pd
from snowflake.snowpark.dataframe import DataFrame

from sqlmesh import ExecutionContext, model

@model(
    "docs_example.snowpark",
    columns={
        "id": "int",
        "name": "text",
        "country": "text",
    },
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> DataFrame:
    # returns the snowpark DataFrame directly, so no data is computed locally
    df = context.snowpark.create_dataframe([[1, "a", "usa"], [2, "b", "cad"]], schema=["id", "name", "country"])
    df = df.filter(df.id > 1)
    return df
```

### Bigframe
This example demonstrates using the [Bigframe](https://cloud.google.com/bigquery/docs/use-bigquery-dataframes#pandas-examples) DataFrame API. If you use Bigquery, the Bigframe API is preferred to Pandas as all computation is done in Bigquery.

```python linenums="1"
import typing as t
from datetime import datetime

from bigframes.pandas import DataFrame

from sqlmesh import ExecutionContext, model


def get_bucket(num: int):
    if not num:
        return "NA"
    boundary = 10
    return "at_or_above_10" if num >= boundary else "below_10"


@model(
    "mart.wiki",
    columns={
        "title": "text",
        "views": "int",
        "bucket": "text",
    },
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> DataFrame:
    # Create a remote function to be used in the Bigframe DataFrame
    remote_get_bucket = context.bigframe.remote_function([int], str)(get_bucket)

    # Returns the Bigframe DataFrame handle, no data is computed locally
    df = context.bigframe.read_gbq("bigquery-samples.wikipedia_pageviews.200809h")

    df = (
        # This runs entirely on the BigQuery engine lazily
        df[df.title.str.contains(r"[Gg]oogle")]
        .groupby(["title"], as_index=False)["views"]
        .sum(numeric_only=True)
        .sort_values("views", ascending=False)
    )

    return df.assign(bucket=df["views"].apply(remote_get_bucket))
```

### Batching
If the output of a Python model is very large and you cannot use Spark, it may be helpful to split the output into multiple batches.

With Pandas or other single machine DataFrame libraries, all data is stored in memory. Instead of returning a single DataFrame instance, you can return multiple instances using the Python generator API. This minimizes the memory footprint by reducing the size of data loaded into memory at any given time.

This examples uses the Python generator `yield` to batch the model output:

```python linenums="1" hl_lines="20"
@model(
    "docs_example.batching",
    columns={
        "id": "int",
    },
)
def execute(
    context: ExecutionContext,
    start: datetime,
    end: datetime,
    execution_time: datetime,
    **kwargs: t.Any,
) -> pd.DataFrame:
    # get the upstream model's table name
    table = context.resolve_table("upstream_model")

    for i in range(3):
        # run 3 queries to get chunks of data and not run out of memory
        df = context.fetchdf(f"SELECT id from {table} WHERE id = {i}")
        yield df
```

## Serialization
SQLMesh executes Python code locally where SQLMesh is running by using our custom [serialization framework](../architecture/serialization.md).
