# Macro variables

Macro variables are placeholders whose values are substituted in when the macro is rendered.

They enable dynamic macro behavior - for example, a date parameter's value might be based on when the macro was run.

!!! note

    This page discusses SQLMesh's built-in macro variables. Learn more about custom, user-defined macro variables on the [SQLMesh macros page](./sqlmesh_macros.md#user-defined-variables).

## Example

Consider a SQL query that filters by date in the `WHERE` clause.

Instead of manually changing the date each time the model is run, you can use a macro variable to make the date dynamic. With the dynamic approach, the date changes automatically based on when the query is run.

This query filters for rows where column `my_date` is after '2023-01-01':

```sql linenums="1"
SELECT *
FROM table
WHERE my_date > '2023-01-01'
```

To make this query's date dynamic you could use the predefined SQLMesh macro variable `@execution_ds`:

```sql linenums="1"
SELECT *
FROM table
WHERE my_date > @execution_ds
```

The `@` symbol tells SQLMesh that `@execution_ds` is a macro variable that requires substitution before the SQL is executed.

The macro variable `@execution_ds` is predefined, so its value will be automatically set by SQLMesh based on when the execution started. If the model was executed on February 1, 2023 the rendered query would be:

```sql linenums="1"
SELECT *
FROM table
WHERE my_date > '2023-02-01'
```

This example used one of SQLMesh's predefined variables, but you can also define your own macro variables.

We describe SQLMesh's predefined variables below; user-defined macro variables are discussed in the [SQLMesh macros](./sqlmesh_macros.md#user-defined-variables) and [Jinja macros](./jinja_macros.md#user-defined-variables) pages.

## Predefined variables
SQLMesh comes with predefined variables that can be used in your queries. They are automatically set by the SQLMesh runtime.

Most predefined variables are related to time and use a combination of prefixes (start, end, etc.) and postfixes (date, ds, ts, etc.). They are described in the next section; [other predefined variables](#runtime-variables) are discussed in the following section.

### Temporal variables

SQLMesh uses the python [datetime module](https://docs.python.org/3/library/datetime.html) for handling dates and times. It uses the standard [Unix epoch](https://en.wikipedia.org/wiki/Unix_time) start of 1970-01-01.

!!! tip "Important"

    Macro instants such as `@start_dt`, `@end_dt`, `@start_tstz`, and `@end_tstz` are always stored and rendered as [UTC](https://en.wikipedia.org/wiki/Coordinated_Universal_Time) timestamps. During incremental backfill, `@start_ds` and `@end_ds` also use UTC calendar dates derived from those interval boundaries.

    Learn more about timezones and incremental models [here](../models/model_kinds.md#timezones).

Relative CLI and API inputs such as `--start "2 weeks ago"` are interpreted using UTC calendar-day boundaries by default. To anchor relative start, end, and execution-time values to a specific timezone (for example, midnight in `America/Los_Angeles`), pass `--time-zone` on supported commands (`plan`, `render`, `evaluate`, `run`, `audit`, `check_intervals`) or set the project-level `time_zone` config. The CLI flag overrides the config value.

When a **day-or-larger** relative start or end (for example, `"1 week ago"`, `"today"`, `"yesterday"`) is parsed with a configured timezone, `@start_tstz` and `@end_tstz` reflect the correct UTC instant and `@start_ds` / `@end_ds` use that timezone's local calendar date in `render`, `evaluate`, and `audit`. Sub-day relatives such as `"2 hours ago"` ignore `--time-zone` and continue to use UTC-relative parsing. Absolute date strings and `@execution_ds` always use UTC calendar dates.

Prefixes:

* start - The inclusive starting interval of a model run
* end - The inclusive end interval of a model run
* execution - The timestamp of when the execution started

Postfixes:

* dt - A python datetime object that converts into a native SQL `TIMESTAMP` (or SQL engine equivalent)
* dtntz - A python datetime object that converts into a native SQL `TIMESTAMP WITHOUT TIME ZONE` (or SQL engine equivalent)
* date - A python date object that converts into a native SQL `DATE`
* ds - A date string with the format: '%Y-%m-%d'
* ts - An ISO 8601 datetime formatted string: '%Y-%m-%d %H:%M:%S'
* tstz - An ISO 8601 datetime formatted string with timezone: '%Y-%m-%d %H:%M:%S%z'
* hour - An integer representing the hour of the day, with values 0-23
* epoch - An integer representing seconds since Unix epoch
* millis - An integer representing milliseconds since Unix epoch

All predefined temporal macro variables:

* dt
    * @start_dt
    * @end_dt
    * @execution_dt

* dtntz
    * @start_dtntz
    * @end_dtntz
    * @execution_dtntz

* date
    * @start_date
    * @end_date
    * @execution_date

* ds
    * @start_ds
    * @end_ds
    * @execution_ds

* ts
    * @start_ts
    * @end_ts
    * @execution_ts

* tstz
    * @start_tstz
    * @end_tstz
    * @execution_tstz

* hour
    * @start_hour
    * @end_hour
    * @execution_hour

* epoch
    * @start_epoch
    * @end_epoch
    * @execution_epoch

* millis
    * @start_millis
    * @end_millis
    * @execution_millis

### Runtime variables

SQLMesh provides additional predefined variables used to modify model behavior based on information available at runtime.

* @runtime_stage - A string value denoting the current stage of the SQLMesh runtime. Typically used in models to conditionally execute pre/post-statements (learn more [here](../models/sql_models.md#optional-prepost-statements)). It returns one of these values:
    * 'loading' - The project is being loaded into SQLMesh's runtime context.
    * 'creating' - The model tables are being created for the first time. The data may be inserted during table creation.
    * 'evaluating' - The model query logic is evaluated, and the data is inserted into the existing model table.
    * 'promoting' - The model is being promoted in the target environment (view created during virtual layer update).
    * 'demoting' - The model is being demoted in the target environment (view dropped during virtual layer update).
    * 'auditing' - The audit is being run.
    * 'testing' - The model query logic is being evaluated in the context of a unit test.
* @gateway - A string value containing the name of the current [gateway](../../guides/connections.md).
* @this_model - The physical table name that the model's view selects from. Typically used to create [generic audits](../audits.md#generic-audits). When used in [on_virtual_update statements](../models/sql_models.md#optional-on-virtual-update-statements), it contains the qualified view name instead.
* @model_kind_name - A string value containing the name of the current model kind. Intended to be used in scenarios where you need to control the [physical properties in model defaults](../../reference/model_configuration.md#model-defaults).

!!! note "Embedding variables in strings"

    Macro variable references sometimes use the curly brace syntax `@{variable}`, which serves a different purpose than the regular `@variable` syntax.

    The curly brace syntax tells SQLMesh that the rendered string should be treated as an identifier, instead of simply replacing the macro variable value.

    For example, if `variable` is defined as `@DEF(`variable`, foo.bar)`, then `@variable` produces `foo.bar`, while `@{variable}` produces `"foo.bar"`. This is because SQLMesh converts `foo.bar` into an identifier, using double quotes to correctly include the `.` character in the identifier name.

    In practice, `@{variable}` is most commonly used to interpolate a value within an identifier, e.g., `@{variable}_suffix`, whereas `@variable` is used to do plain substitutions for string literals.

    Learn more [above](#embedding-variables-in-strings).

#### Before all and after all variables

The following variables are also available in [`before_all` and `after_all` statements](../../guides/configuration.md#before_all-and-after_all-statements), as well as in macros invoked within them.

* @this_env - A string value containing the name of the current [environment](../environments.md).
* @schemas - A list of the schema names of the [virtual layer](../../concepts/glossary.md#virtual-layer) of the current environment.
* @views - A list of the view names of the [virtual layer](../../concepts/glossary.md#virtual-layer) of the current environment.