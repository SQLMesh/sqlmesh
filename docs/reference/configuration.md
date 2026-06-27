# SQLMesh configuration

This page lists SQLMesh configuration options and their parameters. Learn more about SQLMesh configuration in the [configuration guide](../guides/configuration.md).

Configuration options for model definitions are listed in the [model configuration reference page](./model_configuration.md).

## Root configurations

A SQLMesh project configuration consists of root level parameters within which other parameters are defined.

Two important root level parameters are [`gateways`](#gateways) and [gateway/connection defaults](#gatewayconnection-defaults), which have their own sections below.

This section describes the other root level configuration parameters.

### Projects

Configuration options for SQLMesh project directories.

| Option             | Description                                                                                                                 |     Type     | Required |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------- | :----------: | :------: |
| `ignore_patterns`  | Files that match glob patterns specified in this list are ignored when scanning the project folder (Default: `[]`)          | list[string] |    N     |
| `project`          | The project name of this config. Used for [multi-repo setups](../guides/multi_repo.md).                                     | string       |    N     |
| `cache_dir`        | The directory to store the SQLMesh cache. Can be an absolute path or relative to the project directory. (Default: `.cache`) | string       |    N     |
| `log_limit`        | The default number of historical log files to keep (Default: `20`)                                                          | int          |    N     |

### Database (Physical Layer)

Configuration options for how SQLMesh manages database objects in the [physical layer](../concepts/glossary.md#physical-layer).

| Option                        | Description                                                                                                                                                                                                                                                                                        | Type                 | Required |
|-------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:--------------------:|:--------:|
| `snapshot_ttl`                | The period of time that a model snapshot not a part of any environment should exist before being deleted. This is defined as a string with the default `in 1 week`. Other [relative dates](https://dateparser.readthedocs.io/en/latest/) can be used, such as `in 30 days`. (Default: `in 1 week`) | string               | N        |
| `physical_schema_override`    | (Deprecated) Use `physical_schema_mapping` instead. A mapping from model schema names to names of schemas in which physical tables for the corresponding models will be placed.                                                                                                                    | dict[string, string] | N        |
| `physical_schema_mapping`     | A mapping from regular expressions to names of schemas in which physical tables for the corresponding models [will be placed](../guides/configuration.md#physical-table-schemas). (Default physical schema name: `sqlmesh__[model schema]`)                                                        | dict[string, string] | N        |
| `physical_table_naming_convention`| Sets which parts of the model name are included in the physical table names. Options are `schema_and_table`, `table_only` or `hash_md5` - [additional details](../guides/configuration.md#physical-table-naming-convention). (Default: `schema_and_table`)                                     | string               | N        |

### Environments (Virtual Layer)

Configuration options for how SQLMesh manages environment creation and promotion in the [virtual layer](../concepts/glossary.md#virtual-layer).

| Option                        | Description                                                                                                                                                                                                                                                                                        | Type                 | Required |
|-------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:--------------------:|:--------:|
| `environment_ttl`             | The period of time that a development environment should exist before being deleted. This is defined as a string with the default `in 1 week`. Other [relative dates](https://dateparser.readthedocs.io/en/latest/) can be used, such as `in 30 days`. (Default: `in 1 week`)                      | string               | N        |
| `pinned_environments`         | The list of development environments that are exempt from deletion due to expiration                                                                                                                                                                                                               | list[string]         | N        |
| `default_target_environment`  | The name of the environment that will be the default target for the `sqlmesh plan` and `sqlmesh run` commands. (Default: `prod`)                                                                                                                                                                   | string               | N        |
| `environment_suffix_target`   | Whether SQLMesh views should append their environment name to the `schema`, `table` or `catalog` - [additional details](../guides/configuration.md#view-schema-override). (Default: `schema`)                                                                                                      | string               | N        |
| `gateway_managed_virtual_layer`   | Whether SQLMesh views of the virtual layer will be created by the default gateway or model specified gateways - [additional details](../guides/multi_engine.md#gateway-managed-virtual-layer). (Default: False)                                                                                | boolean              | N        |
| `environment_catalog_mapping` | A mapping from regular expressions to catalog names. The catalog name is used to determine the target catalog for a given environment.                                                                                                                                                             | dict[string, string] | N        |
| `virtual_environment_mode` | Determines the Virtual Data Environment (VDE) mode. If set to `full`, VDE is used in both production and development environments. The `dev_only` option enables VDE only in development environments, while in production, no virtual layer is used and models are materialized directly using their original names (i.e., no versioned physical tables). (Default: `full`) | string | N |

### Models

| Option                        | Description                                                                                                                                                                                                                                                                                        | Type                 | Required |
|-------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:--------------------:|:--------:|
| `time_column_format`          | The default format to use for all model time columns. This time format uses [python format codes](https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes) (Default: `%Y-%m-%d`)                                                                                        | string               | N        |
| `infer_python_dependencies`   | Whether SQLMesh will statically analyze Python code to automatically infer Python package requirements. (Default: True)                                                                                                                                                                            | boolean              | N        |
| `model_defaults`              | Default [properties](./model_configuration.md#model-defaults) to set on each model. At a minimum, `dialect` must be set.                                                                                                                                                                           | dict[string, any]    | Y        |

The `model_defaults` key is **required** and must contain a value for the `dialect` key.

See all the keys allowed in `model_defaults` at the [model configuration reference page](./model_configuration.md#model-defaults).

### Variables

The `variables` key can be used to provide values for user-defined variables, accessed using the [`@VAR` macro function](../concepts/macros/sqlmesh_macros.md#global-variables) in SQL model definitions, [`context.var` method](../concepts/models/python_models.md#global-variables) in Python model definitions, and [`evaluator.var` method](../concepts/macros/sqlmesh_macros.md#accessing-global-variable-values) in Python macro functions.

The `variables` key consists of a mapping of variable names to their values - see an example on the [SQLMesh macros concepts page](../concepts/macros/sqlmesh_macros.md#global-variables). Note that keys are case insensitive.

Global variable values may be any of the data types in the table below or lists or dictionaries containing those types.

| Option      | Description                         | Type                                                         | Required |
|-------------|-------------------------------------|:------------------------------------------------------------:|:--------:|
| `variables` | Mapping of variable names to values | dict[string, int \| float \| bool \| string \| list \| dict] | N        |

### Before_all / after_all

The `before_all` and `after_all` keys can be used to specify lists of SQL statements and/or SQLMesh macros that are executed at the start and end, respectively, of the `sqlmesh plan` and `sqlmesh run` commands. For more information and examples, see [the configuration guide](../guides/configuration.md#before_all-and-after_all-statements).

| Option       | Description                                                                          | Type         | Required |
|--------------|--------------------------------------------------------------------------------------|:------------:|:--------:|
| `before_all` | List of SQL statements to be executed at the start of the `plan` and `run` commands. | list[string] |    N     |
| `after_all`  | List of SQL statements to be executed at the end of the `plan` and `run` commands.   | list[string] |    N     |

## Plan

Configuration for the `sqlmesh plan` command.

| Option                    | Description                                                                                                                                                                                                                                             | Type                 | Required |
|---------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:--------------------:|:--------:|
| `auto_categorize_changes` | Indicates whether SQLMesh should attempt to automatically [categorize](../concepts/plans.md#change-categories) model changes during plan creation per each model source type ([additional details](../guides/configuration.md#auto-categorize-changes)) | dict[string, string] | N        |
| `include_unmodified`      | Indicates whether to create views for all models in the target development environment or only for modified ones (Default: False)                                                                                                                       | boolean              | N        |
| `auto_apply`              | Indicates whether to automatically apply a new plan after creation (Default: False)                                                                                                                                                                     | boolean              | N        |
| `forward_only`            | Indicates whether the plan should be [forward-only](../concepts/plans.md#forward-only-plans) (Default: False)                                                                                                                                           | boolean              | N        |
| `enable_preview`          | Indicates whether to enable [data preview](../concepts/plans.md#data-preview) for forward-only models when targeting a development environment (Default: True, except for dbt projects where the target engine does not support cloning)                | Boolean              | N        |
| `no_diff`                 | Don't show diffs for changed models (Default: False)                                                                                                                                                                                                    | boolean              | N        |
| `no_prompts`              | Disables interactive prompts in CLI (Default: True)                                                                                                                                                                                                     | boolean              | N        |
| `always_recreate_environment`              | Always recreates the target environment from the environment specified in `create_from` (by default `prod`) (Default: False)                                                                                                                                                                                                     | boolean              | N        |

## Run

Configuration for the `sqlmesh run` command. Please note that this is only applicable when configured with the [builtin](#builtin) scheduler.

| Option                       | Description                                                                                                        | Type | Required |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------ | :--: | :------: |
| `environment_check_interval` | The number of seconds to wait between attempts to check the target environment for readiness (Default: 30 seconds) | int  |    N     |
| `environment_check_max_wait` | The maximum number of seconds to wait for the target environment to be ready (Default: 6 hours)                    | int  |    N     |

## Format

Formatting settings for the `sqlmesh format` command and UI.

| Option                | Description                                                                                    |  Type   | Required |
| --------------------- | ---------------------------------------------------------------------------------------------- | :-----: | :------: |
| `normalize`           | Whether to normalize SQL (Default: False)                                                      | boolean |    N     |
| `pad`                 | The number of spaces to use for padding (Default: 2)                                           |   int   |    N     |
| `indent`              | The number of spaces to use for indentation (Default: 2)                                       |   int   |    N     |
| `normalize_functions` | Whether to normalize function names. Supported values are: 'upper' and 'lower' (Default: None) | string  |    N     |
| `leading_comma`       | Whether to use leading commas (Default: False)                                                 | boolean |    N     |
| `max_text_width`      | The maximum text width in a segment before creating new lines (Default: 80)                    |   int   |    N     |
| `append_newline`      | Whether to append a newline to the end of the file (Default: False)                            | boolean |    N     |
| `no_rewrite_casts`    | Preserve the existing casts, without rewriting them to use the :: syntax. (Default: False)                  | boolean |    N     |


## Janitor

Configuration for the `sqlmesh janitor` command.

| Option                          | Description                                                                                                                | Type    | Required |
|---------------------------------|----------------------------------------------------------------------------------------------------------------------------|:-------:|:--------:|
| `warn_on_delete_failure`        | Whether to warn instead of erroring if the janitor fails to delete the expired environment schema / views (Default: False) | boolean | N        |
| `expired_snapshots_batch_size`  | Maximum number of expired snapshots to clean in a single batch (Default: 200)                                              | int     | N        |


## UI

SQLMesh UI settings.

| Option           | Description                                                                                   |  Type   | Required |
| ---------------- | --------------------------------------------------------------------------------------------- | :-----: | :------: |
| `format_on_save` | Whether to automatically format model definitions upon saving them to a file (Default: False) | boolean |    N     |

## Gateways

The `gateways` dictionary defines how SQLMesh should connect to the data warehouse, state backend, test backend, and scheduler.

It takes one or more named `gateway` configuration keys, each of which can define its own connections. **Gateway names are case-insensitive** - SQLMesh normalizes all gateway names to lowercase during configuration validation, allowing you to use any case when referencing gateways. A named gateway does not need to specify all four components and will use defaults if any are omitted - more information is provided about [gateway defaults](#gatewayconnection-defaults) below.

For example, a project might configure the `gate1` and `gate2` gateways:

```yaml linenums="1"
gateways:
  gate1:
    connection:
      ...
    state_connection: # defaults to `connection` if omitted
      ...
    test_connection: # defaults to `connection` if omitted
      ...
    scheduler: # defaults to `builtin` if omitted
      ...
  gate2:
    connection:
      ...
```

Find additional information about gateways in the configuration guide [gateways section](../guides/configuration.md#gateways).

### Gateway

Configuration for each named gateway.

#### Connections

A named gateway key may define any or all of a data warehouse connection, state backend connection, state schema name, test backend connection, and scheduler.

Some connections use default values if not specified:

- The `connection` key may be omitted if a [`default_connection`](#default-connectionsscheduler) is specified.
- The state connection defaults to `connection` if omitted.
- The test connection defaults to `connection` if omitted.

NOTE: Spark and Trino engines may not be used for the state connection.

| Option             | Description                                                                                                                                                                     | Type                                                         | Required                                                               |
|--------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:------------------------------------------------------------:|:----------------------------------------------------------------------:|
| `connection`       | The data warehouse connection for core SQLMesh functions.                                                                                                                       | [connection configuration](#connection)                      | N (if [`default_connection`](#default-connectionsscheduler) specified) |
| `state_connection` | The data warehouse connection where SQLMesh will store internal information about the project. (Default: `connection` if using builtin scheduler, otherwise scheduler database) | [connection configuration](#connection)                      | N                                                                      |
| `state_schema`     | The name of the schema where state information should be stored. (Default: `sqlmesh`)                                                                                           | string                                                       | N                                                                      |
| `test_connection`  | The data warehouse connection SQLMesh will use to execute tests. (Default: `connection`)                                                                                        | [connection configuration](#connection)                      | N                                                                      |
| `scheduler`        | The scheduler SQLMesh will use to execute tests. (Default: `builtin`)                                                                                                           | [scheduler configuration](#scheduler)                        | N                                                                      |
| `variables`        | The gateway-specific variables which override the root-level [variables](#variables) by key.                                                                                    | dict[string, int \| float \| bool \| string \| list \| dict] | N                                                                      |

### Connection

Configuration for a data warehouse connection.

Most parameters are specific to the connection engine `type` - see [below](#engine-specific). The default data warehouse connection type is an in-memory DuckDB database.

#### General

| Option              | Description                                                                                                                                                             | Type | Required |
|---------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:----:|:--------:|
| `type`              | The engine type name, listed in engine-specific configuration pages below.                                                                                              | str  |    Y     |
| `concurrent_tasks`  | The maximum number of concurrent tasks that will be run by SQLMesh. (Default: 4 for engines that support concurrent tasks.)                                             | int  |    N     |
| `register_comments` | Whether SQLMesh should register model comments with the SQL engine (if the engine supports it). (Default: `true`.)                                                      | bool |    N     |
| `pre_ping`          | Whether or not to pre-ping the connection before starting a new transaction to ensure it is still alive. This can only be enabled for engines with transaction support. | bool |    N     |
| `pretty_sql`        | If SQL should be formatted before being executed, not recommended in a production setting. (Default: `false`.)                                                          | bool |    N     |

#### Engine-specific

These pages describe the connection configuration options for each execution engine.

* [Athena](../integrations/engines/athena.md)
* [BigQuery](../integrations/engines/bigquery.md)
* [ClickHouse](../integrations/engines/clickhouse.md)
* [Databricks](../integrations/engines/databricks.md)
* [DuckDB](../integrations/engines/duckdb.md)
* [MotherDuck](../integrations/engines/motherduck.md)
* [MySQL](../integrations/engines/mysql.md)
* [MSSQL](../integrations/engines/mssql.md)
* [Postgres](../integrations/engines/postgres.md)
* [GCP Postgres](../integrations/engines/gcp-postgres.md)
* [Redshift](../integrations/engines/redshift.md)
* [Snowflake](../integrations/engines/snowflake.md)
* [Spark](../integrations/engines/spark.md)
* [Trino](../integrations/engines/trino.md)

### Scheduler

Identifies which scheduler backend to use. The scheduler backend is used both for storing metadata and for executing [plans](../concepts/plans.md).

By default, the scheduler type is set to `builtin` and uses the gateway's connection to store metadata.

Below is the list of configuration options specific to each corresponding scheduler type. Find additional details in the [configuration overview scheduler section](../guides/configuration.md#scheduler).

#### Builtin

**Type:** `builtin`

No configuration options are supported by this scheduler type.

## Gateway/connection defaults

The default gateway and connection keys specify what should happen when gateways or connections are not explicitly specified. Find additional details in the configuration overview page [gateway/connection defaults section](../guides/configuration.md#gatewayconnection-defaults).

### Default gateway

If a configuration contains multiple gateways, SQLMesh will use the first one in the `gateways` dictionary by default. The `default_gateway` key is used to specify a different gateway name as the SQLMesh default.

| Option            | Description                                                                                                                  |  Type  | Required |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------- | :----: | :------: |
| `default_gateway` | The name of a gateway to use if one is not provided explicitly (Default: the gateway defined first in the `gateways` option). Gateway names are case-insensitive. | string |    N     |

### Default connections/scheduler

The `default_connection`, `default_test_connection`, and `default_scheduler` keys are used to specify shared defaults across multiple gateways.

For example, you might have a specific connection where your tests should run regardless of which gateway is being used. Instead of duplicating the test connection information in each gateway specification, specify it once in the `default_test_connection` key.

| Option                    | Description                                                                                                                                            |    Type     | Required |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | :---------: | :------: |
| `default_connection`      | The default connection to use if one is not specified in a gateway (Default: A DuckDB connection that creates an in-memory database)                   | connection  |    N     |
| `default_test_connection` | The default connection to use when running tests if one is not specified in a gateway (Default: A DuckDB connection that creates an in-memory database) | connection |    N     |
| `default_scheduler`       | The default scheduler configuration to use if one is not specified in a gateway (Default: built-in scheduler)                                          |  scheduler  |    N     |

## Debug mode

Enable debug mode in one of two ways:

- Pass the `--debug` flag between the CLI command and the subcommand. For example, `sqlmesh --debug plan`.
- Set the `SQLMESH_DEBUG` environment variable to one of the following values: "1", "true", "t", "yes" or "y".

Enabling this mode ensures that full backtraces are printed when using CLI. The default log level is set to `DEBUG` when this mode is enabled.

Example enabling debug mode for the CLI command `sqlmesh plan`:

=== "Bash"

    ```bash
    sqlmesh --debug plan
    ```

    ```bash
    SQLMESH_DEBUG=1 sqlmesh plan
    ```

=== "MS Powershell"

    ```powershell
    sqlmesh --debug plan
    ```

    ```powershell
    $env:SQLMESH_DEBUG=1
    sqlmesh plan
    ```

=== "MS CMD"

    ```cmd
    sqlmesh --debug plan
    ```

    ```cmd
    set SQLMESH_DEBUG=1
    sqlmesh plan
    ```

## Runtime Environment

SQLMesh can run in different runtime environments. For example, you might run it in a regular command-line terminal, in a Jupyter notebook, or in Github's CI/CD platform.

When it starts up, SQLMesh automatically detects the runtime environment and adjusts its behavior accordingly. For example, it registers `%magic` commands if in a Jupyter notebook and adjusts logging behavior if in a CI/CD environment.

If necessary, you may force SQLMesh to use a specific runtime environment by setting the `SQLMESH_RUNTIME_ENVIRONMENT` environment variable.

It accepts the following values, which will cause SQLMesh to behave as if it were in the runtime environment in parentheses:

- `terminal` (CLI console)
- `databricks` (Databricks notebook)
- `google_colab` (Google Colab notebook)
- `jupyter` (Jupyter notebook)
- `debugger` (Debugging output)
- `ci` (CI/CD or other non-interactive environment)

## Anonymized usage information

We strive to make SQLMesh the best data transformation tool on the market. Part of accomplishing that is continually fixing bugs, adding features, and improving SQLMesh's performance.

We prioritize our development work based on the needs of SQLMesh users. Some users share their needs via our Slack or Github communities, but many do not. We have added some simple anonymized usage information (telemetry) to SQLMesh to ensure the needs of all users are heard.

All information is anonymized with hash functions, so we could not link data to a specific company, user, or project even if we wanted to (which we don't!). No information is related to credentials or authentication.

We collect anonymized information about SQLMesh project complexity and usage - for example, number of models, count of model kinds, project load time, whether an error occurred during a plan/run (no stacktraces or error message), and names (but not values) of the arguments passed to CLI commands.

You can disable collection of anonymized usage information with these methods:

- Set the root `disable_anonymized_analytics: true` key in your SQLMesh project configuration file
- Execute SQLMesh commands with an environment variable `SQLMESH__DISABLE_ANONYMIZED_ANALYTICS` set to `1`, `true`, `t`, `yes`, or `y`

## Parallel loading
SQLMesh by default uses all of your cores when loading models and snapshots. It takes advantage of `fork` which is not available on Windows. The default is to use the same number of workers as cores on your machine if fork is available.

You can override this setting by setting the environment variable `MAX_FORK_WORKERS`. A value of 1 will disable forking and load things sequentially.
