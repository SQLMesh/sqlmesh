# Configuration guide

SQLMesh's behavior is determined by three things: a project's files (e.g., models), user actions (e.g., creating a `plan`), and how SQLMesh is configured.

This page describes how SQLMesh configuration works and discusses the aspects of SQLMesh behavior that can be modified via configuration.

The [configuration reference page](../reference/configuration.md) contains concise lists of all configuration parameters and their default values.

## Configuration files

**NOTE:** SQLMesh project configurations have the following two requirements:

1. A `config.yaml` or `config.py` file must be present in the project's folder.
2. That configuration file must contain a default SQL dialect for the project's models in the [`model_defaults` `dialect` key](#models).

SQLMesh configuration parameters can be set as environment variables, in a configuration file in the `~/.sqlmesh` folder, and in the configuration file within a project folder.

The sources have the following order of precedence:

1. Environment variable (e.g., `SQLMESH__MODEL_DEFAULTS__DIALECT`). [HIGHEST PRECEDENCE]
2. `config.yaml` or `config.py` in the `~/.sqlmesh` folder.
3. `config.yaml` or `config.py` in a project folder. [LOWEST PRECEDENCE]

!!! note
    To relocate the `.sqlmesh` folder, set the `SQLMESH_HOME` environment variable to your preferred directory path.

### File type

You can specify a SQLMesh configuration in either YAML or Python.

YAML configuration is simpler, and we recommend it for most projects. Python configuration is more complex, but it enables functionality that YAML does not support.

Because Python configuration files are evaluated by Python when SQLMesh reads them, they support dynamic parameters based on the computational environment in which SQLMesh is running.

For example, Python configuration files enable use of third-party secrets managers for storing passwords and other sensitive information. They also support user-specific parameters such as automatically setting project defaults based on which user account is running SQLMesh.

#### YAML

YAML configuration files consist of configuration keys and values. Strings are not quoted, and some keys are "dictionaries" that contain one or more sub-keys.

For example, the `default_gateway` key specifies the default gateway SQLMesh should use when executing commands. It takes a single, unquoted gateway name as its value:

```yaml linenums="1"
default_gateway: local
```

In contrast, the `gateways` key takes dictionaries as values, and each gateway dictionary contains one or more connection dictionaries. This example specifies the `my_gateway` gateway with a Snowflake `connection`:

```yaml linenums="1"
gateways:
  my_gateway:
    connection:
      type: snowflake
      user: <username>
      password: <password>
      account: <account>
```

Gateway dictionaries can contain multiple connection dictionaries if different SQLMesh components should use different connections (e.g., SQLMesh `test`s should run in a different database than SQLMesh `plan`s). See the [gateways section](#gateways) for more information on gateway configuration.

#### Python

Python configuration files consist of statements that import SQLMesh configuration classes and a configuration specification using those classes.

At minimum, a Python configuration file must:

1. Create an object of the SQLMesh `Config` class named `config`
2. Specify that object's `model_defaults` argument with a `ModelDefaultsConfig()` object specifying the default SQL dialect for the project's models

For example, this minimal configuration specifies a default SQL dialect of `duckdb` and uses the default values for all other configuration parameters:

```python linenums="1"
from sqlmesh.core.config import Config, ModelDefaultsConfig

config = Config(
    model_defaults=ModelDefaultsConfig(dialect="duckdb"),
)
```

Python configuration files may optionally define additional configuration objects and switch between the configurations when issuing `sqlmesh` commands. For example, if a configuration file contained a second configuration object `my_second_config`, you could create a plan using that config with `sqlmesh --config my_second_config plan`.

Different `Config` arguments accept different object types. Some, such as `model_defaults`, take SQLMesh configuration objects. Others, such as `default_gateway`, take strings or other Python object types like dictionaries.

SQLMesh's Python configuration components are documented in the `sqlmesh.core.config` module's [API documentation](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/config.html).

The `config` sub-module API documentation describes the individual classes used for the relevant `Config` arguments:

- [Model defaults configuration](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/config/model.html): `ModelDefaultsConfig()`
- [Gateway configuration](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/config/gateway.html): `GatewayConfig()`
    - [Connection configuration](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/config/connection.html) (separate classes for each supported database/engine)
    - [Scheduler configuration](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/config/scheduler.html) (separate classes for each supported scheduler)
- [Plan change categorization configuration](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/config/categorizer.html#CategorizerConfig): `CategorizerConfig()`
- [User configuration](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/user.html#User): `User()`
- [Notification configuration](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/notification_target.html) (separate classes for each notification target)

See the [notifications guide](../guides/notifications.md) for more information about user and notification specification.

## Environment variables

All software runs within a system environment that stores information as "environment variables."

SQLMesh can access environment variables during configuration, which enables approaches like storing passwords/secrets outside the configuration file and changing configuration parameters dynamically based on which user is running SQLMesh.

You can specify environment variables in the configuration file or by storing them in a `.env` file.

### .env files

SQLMesh automatically loads environment variables from a `.env` file in your project directory. This provides a convenient way to manage environment variables without having to set them in your shell.

Create a `.env` file in your project root with key-value pairs:

```bash
# .env file
SNOWFLAKE_PW=my_secret_password
S3_BUCKET=s3://my-data-bucket/warehouse
DATABASE_URL=postgresql://user:pass@localhost/db

# Override specific SQLMesh configuration values
SQLMESH__DEFAULT_GATEWAY=production
SQLMESH__MODEL_DEFAULTS__DIALECT=snowflake
```

See the [overrides](#overrides) section for a detailed explanation of how these are defined.

The rest of the `.env` file variables can be used in your configuration files with `{{ env_var('VARIABLE_NAME') }}` syntax in YAML or accessed via `os.environ['VARIABLE_NAME']` in Python.

#### Custom dot env file location and name

By default, SQLMesh loads `.env` files from each project directory. However, you can specify a custom path using the `--dotenv` CLI flag directly when running a command:

```bash
sqlmesh --dotenv /path/to/custom/.env plan
```

!!! note
    The `--dotenv` flag is a global option and must be placed **before** the subcommand (e.g. `plan`, `run`), not after.

Alternatively, you can export the `SQLMESH_DOTENV_PATH` environment variable once, to persist a custom path across all subsequent commands in your shell session:

```bash
export SQLMESH_DOTENV_PATH=/path/to/custom/.custom_env
sqlmesh plan
sqlmesh run
```

**Important considerations:**
- Add `.env` to your `.gitignore` file to avoid committing sensitive information
- SQLMesh will only load the `.env` file if it exists in the project directory (unless a custom path is specified)
- When using a custom path, that specific file takes precedence over any `.env` file in the project directory.

### Configuration file

This section demonstrates using environment variables in YAML and Python configuration files.

The examples specify a Snowflake connection whose password is stored in an environment variable `SNOWFLAKE_PW`.

=== "YAML"

    Specify environment variables in a YAML configuration with the syntax `{{ env_var('<ENVIRONMENT VARIABLE NAME>') }}`. Note that the environment variable name is contained in single quotes.

    Access the `SNOWFLAKE_PW` environment variable in a Snowflake connection configuration like this:

    ```yaml linenums="1"
    gateways:
      my_gateway:
        connection:
          type: snowflake
          user: <username>
          password: {{ env_var('SNOWFLAKE_PW') }}
          account: <account>
    ```

=== "Python"

    Python accesses environment variables via the `os` library's `environ` dictionary.

    Access the `SNOWFLAKE_PW` environment variable in a Snowflake connection configuration like this:

    ```python linenums="1"
    import os
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        SnowflakeConnectionConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                connection=SnowflakeConnectionConfig(
                    user=<username>,
                    password=os.environ['SNOWFLAKE_PW'],
                    account=<account>,
                ),
            ),
        }
    )
    ```

#### Default target environment

The SQLMesh `plan` command acts on the `prod` environment by default (i.e., `sqlmesh plan` is equivalent to `sqlmesh plan prod`).

In some organizations, users never run plans directly against `prod` - they do all SQLMesh work in a development environment unique to them. In a standard SQLMesh configuration, this means they need to include their development environment name every time they issue the `plan` command (e.g., `sqlmesh plan dev_tony`).

If your organization works like this, it may be convenient to change the `plan` command's default environment from `prod` to each user's development environment. That way people can issue `sqlmesh plan` without typing the environment name every time.

The SQLMesh configuration `user()` function returns the name of the user currently logged in and running SQLMesh. It retrieves the username from system environment variables like `USER` on MacOS/Linux or `USERNAME` on Windows.

Call `user()` inside Jinja curly braces with the syntax `{{ user() }}`, which allows you to combine the user name with a prefix or suffix.

The example configuration below constructs the environment name by appending the username to the end of the string `dev_`. If the user running SQLMesh is `tony`, the default target environment when they run SQLMesh will be `dev_tony`. In other words, `sqlmesh plan` will be equivalent to `sqlmesh plan dev_tony`.

=== "YAML"

    Default target environment is `dev_` combined with the username running SQLMesh.

    ```yaml
    default_target_environment: dev_{{ user() }}
    ```

=== "Python"

    Default target environment is `dev_` combined with the username running SQLMesh.

    Retrieve the username with the `getpass.getuser()` function, and combine it with `dev_` in a Python f-string.

    ```python linenums="1" hl_lines="1 17"
    import getpass
    import os
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        SnowflakeConnectionConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        gateways={
            "my_gateway": GatewayConfig(
                connection=DuckDBConnectionConfig(),
            ),
        },
        default_target_environment=f"dev_{getpass.getuser()}",
    )
    ```

### Overrides

Environment variables have the highest precedence among configuration methods, as [noted above](#configuration-files). They will automatically override configuration file specifications if they follow a specific naming structure.

The structure is based on the names of the configuration fields, with double underscores `__` between the field names. The environment variable name must begin with `SQLMESH__`, followed by the YAML field names starting at the root and moving downward in the hierarchy.

For example, we can override the password specified in a Snowflake connection. This is the YAML specification contained in our configuration file, which specifies a password `dummy_pw`:

```yaml linenums="1"
gateways:
  my_gateway:
    connection:
      type: snowflake
      user: <username>
      password: dummy_pw
      account: <account>
```

We can override the `dummy_pw` value with the true password `real_pw` by creating the environment variable. This example demonstrates creating the variable with the bash `export` function:

```bash
export SQLMESH__GATEWAYS__MY_GATEWAY__CONNECTION__PASSWORD="real_pw"
```

After the initial string `SQLMESH__`, the environment variable name components move down the key hierarchy in the YAML specification: `GATEWAYS` --> `MY_GATEWAY` --> `CONNECTION` --> `PASSWORD`.

## Configuration types

A SQLMesh project configuration is hierarchical and consists of root level parameters within which other parameters are defined.

Conceptually, we can group the root level parameters into the following types. Each type links to its table of parameters in the [SQLMesh configuration reference page](../reference/configuration.md):

1. [Project](../reference/configuration.md#projects) - configuration options for SQLMesh project directories.
2. [Environment](../reference/configuration.md#environments) - configuration options for SQLMesh environment creation/promotion, physical table schemas, and view schemas.
3. [Gateways](../reference/configuration.md#gateways) - configuration options for how SQLMesh should connect to the data warehouse, state backend, and scheduler.
4. [Gateway/connection defaults](../reference/configuration.md#gatewayconnection-defaults) - configuration options for what should happen when gateways or connections are not all explicitly specified.
5. [Model defaults](../reference/model_configuration.md) - configuration options for what should happen when model-specific configurations are not explicitly specified in a model's file.
6. [Debug mode](../reference/configuration.md#debug-mode) - configuration option for SQLMesh to print and log actions and full backtraces.

## Configuration details

The rest of this page provides additional detail for some of the configuration options and provides brief examples. Comprehensive lists of configuration options are at the [configuration reference page](../reference/configuration.md).

### Cache directory

By default, the SQLMesh cache is stored in a `.cache` directory within your project folder. You can customize the cache location using the `cache_dir` configuration option:

=== "YAML"

    ```yaml linenums="1"
    # Relative path to project directory
    cache_dir: my_custom_cache

    # Absolute path
    cache_dir: /tmp/sqlmesh_cache

    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        cache_dir="/tmp/sqlmesh_cache",
    )
    ```

The cache directory is automatically created if it doesn't exist. You can clear the cache using the `sqlmesh clean` command.

### Table/view storage locations

SQLMesh creates schemas, physical tables, and views in the data warehouse/engine. Learn more about why and how SQLMesh creates schema in the ["Why does SQLMesh create schemas?" FAQ](../faq/faq.md#schema-question).

The default SQLMesh behavior described in the FAQ is appropriate for most deployments, but you can override *where* SQLMesh creates physical tables and views with the `physical_schema_mapping`, `environment_suffix_target`, and `environment_catalog_mapping` configuration options.

You can also override *what* the physical tables are called by using the `physical_table_naming_convention` option.

These options are in the [environments](../reference/configuration.md#environments) section of the configuration reference page.

#### Physical table schemas
By default, SQLMesh creates physical schemas for a model with a naming convention of `sqlmesh__[model schema]`.

This can be overridden on a per-schema basis using the `physical_schema_mapping` option, which removes the `sqlmesh__` prefix and uses the [regex pattern](https://docs.python.org/3/library/re.html#regular-expression-syntax) you provide to map the schemas defined in your model to their corresponding physical schemas.

This example configuration overrides the default physical schemas for the `my_schema` model schema and any model schemas starting with `dev`:

=== "YAML"

    ```yaml linenums="1"
    physical_schema_mapping:
      '^my_schema$': my_new_schema,
      '^dev.*': development
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        physical_schema_mapping={
            "^my_schema$": "my_new_schema",
            '^dev.*': "development"
        },
    )
    ```

This config causes the following mapping behaviour:

| Model name            | Default physical location                 | Resolved physical location
| --------------------- | ----------------------------------------- | ------------------------------------ |
| `my_schema.my_table`  | `sqlmesh__my_schema.table_<fingerprint>`  | `my_new_schema.table_<fingerprint>`  |
| `dev_schema.my_table` | `sqlmesh__dev_schema.table_<fingerprint>` | `development.table_<fingerprint>`    |
| `other.my_table`      | `sqlmesh__other.table_<fingerprint>`      | `sqlmesh__other.table_<fingerprint>` |


This only applies to the _physical tables_ that SQLMesh creates - the views are still created in `my_schema` (prod) or `my_schema__<env>`.

#### Disable environment-specific schemas

SQLMesh stores `prod` environment views in the schema in a model's name - for example, the `prod` views for a model `my_schema.users` will be located in `my_schema`.

By default, for non-prod environments SQLMesh creates a new schema that appends the environment name to the model name's schema. For example, by default the view for a model `my_schema.users` in a SQLMesh environment named `dev` will be located in the schema `my_schema__dev` as `my_schema__dev.users`.

##### Show at the table level instead

This behavior can be changed to append a suffix at the end of a _table/view_ name instead. Appending the suffix to a table/view name means that non-prod environment views will be created in the same schema as the `prod` environment. The prod and non-prod views are differentiated by non-prod view names ending with `__<env>`.

For example, if you created a `dev` environment for a project containing a model named `my_schema.users`, the model view would be created as `my_schema.users__dev` instead of the default behavior of `my_schema__dev.users`.

Config example:

=== "YAML"

    ```yaml linenums="1"
    environment_suffix_target: table
    ```

=== "Python"

    The Python `environment_suffix_target` argument takes an `EnvironmentSuffixTarget` enumeration with a value of `EnvironmentSuffixTarget.TABLE`, `EnvironmentSuffixTarget.CATALOG` or `EnvironmentSuffixTarget.SCHEMA` (default).

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig, EnvironmentSuffixTarget

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        environment_suffix_target=EnvironmentSuffixTarget.TABLE,
    )
    ```

!!! info "Default behavior"
    The default behavior of appending the suffix to schemas is recommended because it leaves production with a single clean interface for accessing the views. However, if you are deploying SQLMesh in an environment with tight restrictions on schema creation then this can be a useful way of reducing the number of schemas SQLMesh uses.

##### Show at the catalog level instead

If neither the schema (default) nor the table level are sufficient for your use case, you can indicate the environment at the catalog level instead.

This can be useful if you have downstream BI reporting tools and you would like to point them at a development environment to test something out without renaming all the table / schema references within the report query.

In order to achieve this, you can configure [environment_suffix_target](../reference/configuration.md#environments) like so:

=== "YAML"

    ```yaml linenums="1"
    environment_suffix_target: catalog
    ```

=== "Python"

    The Python `environment_suffix_target` argument takes an `EnvironmentSuffixTarget` enumeration with a value of `EnvironmentSuffixTarget.TABLE`, `EnvironmentSuffixTarget.CATALOG` or `EnvironmentSuffixTarget.SCHEMA` (default).

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig, EnvironmentSuffixTarget

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        environment_suffix_target=EnvironmentSuffixTarget.CATALOG,
    )
    ```

Given the example of a model called `my_schema.users` with a default catalog of `warehouse` this will cause the following behavior:

- For the `prod` environment, the default catalog as configured in the gateway will be used. So the view will be created at `warehouse.my_schema.users`
- For any other environment, eg `dev`, the environment name will be appended to the default catalog. So the view will be created at `warehouse__dev.my_schema.users`
- If a model is fully qualified with a catalog already, eg `finance_mart.my_schema.users`, then the environment catalog will be based off the model catalog and not the default catalog. In this example, the view will be created at `finance_mart__dev.my_schema.users`


!!! warning "Caveats"
    - Using `environment_suffix_target: catalog` only works on engines that support querying across different catalogs. If your engine does not support cross-catalog queries then you will need to use `environment_suffix_target: schema` or `environment_suffix_target: table` instead.
    - Automatic catalog creation is not supported on all engines even if they support cross-catalog queries. For engines where it is not supported, the catalogs must be managed externally from SQLMesh and exist prior to invoking SQLMesh.

#### Physical table naming convention

Out of the box, SQLMesh has the following defaults set:

 - `environment_suffix_target: schema`
 - `physical_table_naming_convention: schema_and_table`
 - no `physical_schema_mapping` overrides, so a `sqlmesh__<model schema>` physical schema will be created for each model schema

This means that given a catalog of `warehouse` and a model named `finance_mart.transaction_events_over_threshold`, SQLMesh will create physical tables using the following convention:

```
# <catalog>.sqlmesh__<schema>.<schema>__<table>__<fingerprint>

warehouse.sqlmesh__finance_mart.finance_mart__transaction_events_over_threshold__<fingerprint>
```

This deliberately contains some redundancy with the *model* schema as it's repeated at the physical layer in both the physical schema name as well as the physical table name.

This default exists to make the physical table names portable between different configurations. If you were to define a `physical_schema_mapping` that maps all models to the same physical schema, since the model schema is included in the table name as well, there are no naming conflicts.

##### Table only

Some engines have object name length limitations which cause them to [silently truncate](https://www.postgresql.org/docs/current/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS) table and view names that exceed this limit. This behaviour breaks SQLMesh, so we raise a runtime error if we detect the engine would silently truncate the name of the table we are trying to create.

Having redundancy in the physical table names does reduce the number of characters that can be utilised in model names. To increase the number of characters available to model names, you can use `physical_table_naming_convention` like so:

=== "YAML"

    ```yaml linenums="1"
    physical_table_naming_convention: table_only
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig, TableNamingConvention

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        physical_table_naming_convention=TableNamingConvention.TABLE_ONLY,
    )
    ```

This will cause SQLMesh to omit the model schema from the table name and generate physical names that look like (using the above example):
```
# <catalog>.sqlmesh__<schema>.<table>__<fingerprint>

warehouse.sqlmesh__finance_mart.transaction_events_over_threshold__<fingerprint>
```

Notice that the model schema name is no longer part of the physical table name. This allows for slightly longer model names on engines with low identifier length limits, which may be useful for your project.

In this configuration, it is your responsibility to ensure that any schema overrides in `physical_schema_mapping` result in each model schema getting mapped to a unique physical schema.

For example, the following configuration will cause **data corruption**:

```yaml
physical_table_naming_convention: table_only
physical_schema_mapping:
  '.*': sqlmesh
```

This is because every model schema is mapped to the same physical schema but the model schema name is omitted from the physical table name.

##### MD5 hash

If you *still* need more characters, you can set `physical_table_naming_convention: hash_md5` like so:

=== "YAML"

    ```yaml linenums="1"
    physical_table_naming_convention: hash_md5
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig, TableNamingConvention

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        physical_table_naming_convention=TableNamingConvention.HASH_MD5,
    )
    ```

This will cause SQLMesh generate physical names that are always 45-50 characters in length and look something like:

```
# sqlmesh_md5__<hash of what we would have generated using 'schema_and_table'>

sqlmesh_md5__d3b07384d113edec49eaa6238ad5ff00

# or, for a dev preview
sqlmesh_md5__d3b07384d113edec49eaa6238ad5ff00__dev
```

This has a downside that now it's much more difficult to determine which table corresponds to which model by just looking at the database with a SQL client. However, the table names have a predictable length so there are no longer any surprises with identfiers exceeding the max length at the physical layer.

#### Virtual Data Environment Modes

By default, Virtual Data Environments (VDE) are applied across both development and production environments. This allows SQLMesh to reuse physical tables when appropriate, even when promoting from development to production.

However, users may prefer their production environment to be non-virtual. The non-exhaustive list of reasons may include:

- Integration with third-party tools and platforms, such as data catalogs, may not work well with the virtual view layer that SQLMesh imposes by default
- A desire to rely on time travel features provided by cloud data warehouses such as BigQuery, Snowflake, and Databricks

To mitigate this, SQLMesh offers an alternative 'dev-only' mode for using VDE. It can be enabled in the project configuration like so:

=== "YAML"

    ```yaml linenums="1"
    virtual_environment_mode: dev_only
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config

    config = Config(
        virtual_environment_mode="dev_only",
    )
    ```

'dev-only' mode means that VDE is applied only in development environments. While in production, model tables and views are updated directly and bypass the virtual layer. This also means that physical tables in production will be created using the original, **unversioned** model names. Users will still benefit from VDE and data reuse across development environments.

Please note the following tradeoffs when enabling this mode:

- All data inserted in development environments is used only for [preview](../concepts/plans.md#data-preview-for-forward-only-changes) and will **not** be reused in production
- Reverting a model to a previous version will be applied going forward and may require an explicit data restatement

!!! warning
    Switching the mode for an existing project will result in a **complete rebuild** of all models in the project. Refer to the [Table Migration Guide](./table_migration.md) to migrate existing tables without rebuilding them from scratch.


#### Environment view catalogs

By default, SQLMesh creates an environment view in the same [catalog](../concepts/glossary.md#catalog) as the physical table the view points to. The physical table's catalog is determined by either the catalog specified in the model name or the default catalog defined in the connection.

It can be desirable to create `prod` and non-prod virtual layer objects in separate catalogs instead. For example, there might be a "prod" catalog that contains all `prod` environment views and a separate "dev" catalog that contains all `dev` environment views.

Separate prod and non-prod catalogs can also be useful if you have a CI/CD pipeline that creates environments, like the [SQLMesh Github Actions CI/CD Bot](../integrations/github.md). You might want to store the CI/CD environment objects in a dedicated catalog since there can be many of them.

!!! info "Virtual layer only"
    Note that the following setting only affects the [virtual layer](../concepts/glossary.md#virtual-layer). If you need full segregation by catalog between environments in the [physical layer](../concepts/glossary.md#physical-layer) as well, see the [Isolated Systems Guide](../guides/isolated_systems.md).

To configure separate catalogs, provide a mapping from [regex patterns](https://en.wikipedia.org/wiki/Regular_expression) to catalog names. SQLMesh will compare the name of an environment to the regex patterns; when it finds a match it will store the environment's objects in the corresponding catalog.

SQLMesh evaluates the regex patterns in the order defined in the configuration; it uses the catalog for the first matching pattern. If no match is found, the catalog defined in the model or the default catalog defined on the connection will be used.

Config example:

=== "YAML"

    ```yaml linenums="1"
    environment_catalog_mapping:
      '^prod$': prod
      '^dev.*': dev
      '^analytics_repo.*': cicd
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        environment_catalog_mapping={
            '^prod$': 'prod',
            '^dev.*': 'dev',
            '^analytics_repo.*': 'cicd',
        },
    )
    ```

With the example configuration above, SQLMesh would evaluate environment names as follows:

* If the environment name is `prod`, the catalog will be `prod`.
* If the environment name starts with `dev`, the catalog will be `dev`.
* If the environment name starts with `analytics_repo`, the catalog will be `cicd`.

!!! warning
    This feature is mutually exclusive with `environment_suffix_target: catalog` in order to prevent ambiguous mappings from being defined. Attempting to specify both `environment_catalog_mapping` and `environment_suffix_target: catalog` will raise an error on project load

*Note:* This feature is only available for engines that support querying across catalogs. At the time of writing, the following engines are **NOT** supported:

* [MySQL](../integrations/engines/mysql.md)
* [Postgres](../integrations/engines/postgres.md)
* [GCP Postgres](../integrations/engines/gcp-postgres.md)

##### Regex Tips
* If you are less familiar with regex, you can use a tool like [regex101](https://regex101.com/) to help you build your regex patterns.
    * LLMs, like [ChatGPT](https://chat.openai.com), can help with generating regex patterns. Make sure to validate the suggestion in regex101.
* If you are wanting to do an exact word match then surround it with `^` and `$` like in the example above.
* If you want a catch-all at the end of your mapping, to avoid ever using the model catalog or default catalog, then use `.*` as the pattern. This will match any environment name that hasn't already been matched.


### Auto-categorize model changes

SQLMesh compares the current state of project files to an environment when `sqlmesh plan` is run. It detects changes to models, which can be classified as breaking or non-breaking.

SQLMesh can  attempt to automatically [categorize](../concepts/plans.md#change-categories) the changes it detects. The `plan.auto_categorize_changes` option determines whether SQLMesh should attempt automatic change categorization. This option is in the [plan](../reference/configuration.md#plan) section of the configuration reference page.

Supported values:

* `full`: Never prompt the user for input, instead fall back to the most conservative category ([breaking](../concepts/plans.md#breaking-change)) if the category can't be determined automatically.
* `semi`: Prompt the user for input only if the change category can't be determined automatically.
* `off`: Always prompt the user for input; automatic categorization will not be attempted.

Example showing default values:

=== "YAML"

    ```yaml linenums="1"
    plan:
      auto_categorize_changes:
        external: full
        python: off
        sql: full
        seed: full
    ```

=== "Python"

    The Python `auto_categorize_changes` argument takes `CategorizerConfig` object. That object's arguments take an `AutoCategorizationMode` enumeration with values of `AutoCategorizationMode.FULL`, `AutoCategorizationMode.SEMI`, or `AutoCategorizationMode.OFF`.

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        AutoCategorizationMode,
        CategorizerConfig,
        PlanConfig,
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        plan=PlanConfig(
            auto_categorize_changes=CategorizerConfig(
                external=AutoCategorizationMode.FULL,
                python=AutoCategorizationMode.OFF,
                sql=AutoCategorizationMode.FULL,
                seed=AutoCategorizationMode.FULL,
            )
        ),
    )
    ```


### Always comparing against production

By default, SQLMesh compares the current state of project files to the target `<env>` environment when `sqlmesh plan <env>` is run. However, a common expectation is that local changes should always be compared to the production environment.

The `always_recreate_environment` boolean plan option can alter this behavior. When enabled, SQLMesh will always attempt to compare against the production environment by recreating the target environment; If `prod` does not exist, SQLMesh will fall back to comparing against the target environment.

**NOTE:**: Upon succesfull plan application, changes are still promoted to the target `<env>` environment.

=== "YAML"

    ```yaml linenums="1"
    plan:
        always_recreate_environment: True
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        PlanConfig,
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        plan=PlanConfig(
            always_recreate_environment=True,
        ),
    )
    ```

#### Change Categorization Example

Consider this scenario with `always_recreate_environment` enabled:

1. Initial state in `prod`:
```sql
MODEL (name sqlmesh_example.test_model, kind FULL);
SELECT 1 AS col
```

1. First (breaking) change in `dev`:
```sql
MODEL (name sqlmesh_example__dev.test_model, kind FULL);
SELECT 2 AS col
```

??? "Output plan example #1"

    ```bash
    New environment `dev` will be created from `prod`

    Differences from the `prod` environment:

    Models:
    └── Directly Modified:
        └── sqlmesh_example__dev.test_model

    ---
    +++


    kind FULL
    )
    SELECT
    -  1 AS col
    +  2 AS col
    ```

3. Second (metadata) change in `dev`:
```sql
MODEL (name sqlmesh_example__dev.test_model, kind FULL, owner 'John Doe');
SELECT 5 AS col
```

??? "Output plan example #2"

    ```bash
    New environment `dev` will be created from `prod`

    Differences from the `prod` environment:

    Models:
    └── Directly Modified:
        └── sqlmesh_example__dev.test_model

    ---

    +++

    @@ -1,8 +1,9 @@

    MODEL (
    name sqlmesh_example.test_model,
    +  owner "John Doe",
    kind FULL
    )
    SELECT
    -  1 AS col
    +  2 AS col

    Directly Modified: sqlmesh_example__dev.test_model (Breaking)
    Models needing backfill:
    └── sqlmesh_example__dev.test_model: [full refresh]
    ```

Even though the second change should have been a metadata change (thus not requiring a backfill), it will still be classified as a breaking change because the comparison is against production instead of the previous development state. This is intentional and may cause additional backfills as more changes are accumulated.


### Gateways

The `gateways` configuration defines how SQLMesh should connect to the data warehouse, state backend, and scheduler. These options are in the [gateway](../reference/configuration.md#gateway) section of the configuration reference page.

Each gateway key represents a unique gateway name and configures its connections. **Gateway names are case-insensitive** - SQLMesh automatically normalizes gateway names to lowercase during configuration validation. This means you can use any case in your configuration files (e.g., `MyGateway`, `mygateway`, `MYGATEWAY`) and they will all work correctly.

For example, this configures the `my_gateway` gateway:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        connection:
          ...
        state_connection:
          ...
        test_connection:
          ...
        scheduler:
          ...
    ```

=== "Python"

    The Python `gateways` argument takes a dictionary of gateway names and `GatewayConfig` objects. A `GatewayConfig`'s connection-related arguments take an [engine-specific connection config](#engine-connection-configuration) object, and the `scheduler` argument takes a [scheduler config](#scheduler) object.


    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        ...
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                connection=...,
                state_connection=...,
                test_connection=...,
                scheduler=...,
            ),
        }
    )
    ```

Gateways do not need to specify all four components in the example above. The gateway defaults options control what happens if they are not all specified - find more information on [gateway defaults below](#gatewayconnection-defaults).

### Connections

The `connection` configuration controls the data warehouse connection. These options are in the [connection](../reference/configuration.md#connection) section of the configuration reference page.

The allowed keys include:

- The optional [`concurrent_tasks`](#concurrent-tasks) key specifies the maximum number of concurrent tasks SQLMesh will run. Default value is 4 for engines that support concurrent tasks.
- Most keys are specific to the connection engine `type` - see [below](#engine-connection-configuration). The default data warehouse connection type is an in-memory DuckDB database.

Example snowflake connection configuration:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        connection:
          type: snowflake
          user: <username>
          password: <password>
          account: <account>
    ```

=== "Python"

    A Snowflake connection is specified with a `SnowflakeConnectionConfig` object.

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        SnowflakeConnectionConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                connection=SnowflakeConnectionConfig(
                    user=<username>,
                    password=<password>,
                    account=<account>,
                ),
            ),
        }
    )
    ```

#### Engine connection configuration

These pages describe the connection configuration options for each execution engine.

* [Athena](../integrations/engines/athena.md)
* [BigQuery](../integrations/engines/bigquery.md)
* [Databricks](../integrations/engines/databricks.md)
* [DuckDB](../integrations/engines/duckdb.md)
* [Fabric](../integrations/engines/fabric.md)
* [MotherDuck](../integrations/engines/motherduck.md)
* [MySQL](../integrations/engines/mysql.md)
* [MSSQL](../integrations/engines/mssql.md)
* [Postgres](../integrations/engines/postgres.md)
* [GCP Postgres](../integrations/engines/gcp-postgres.md)
* [Redshift](../integrations/engines/redshift.md)
* [Snowflake](../integrations/engines/snowflake.md)
* [StarRocks](../integrations/engines/starrocks.md)
* [Spark](../integrations/engines/spark.md)
* [Trino](../integrations/engines/trino.md)

#### State connection

Configuration for the state backend connection if different from the data warehouse connection.

The data warehouse connection is used to store SQLMesh state if the `state_connection` key is not specified.

Unlike data transformations, storing state information requires database transactions. Data warehouses aren’t optimized for executing transactions, and storing state information in them can slow down your project or produce corrupted data due to simultaneous writes to the same table. Therefore, production SQLMesh deployments should use a dedicated state connection.

!!! note
    Using the same connection for data warehouse and state is not recommended for production deployments of SQLMesh.

The easiest and most reliable way to manage your state connection is for [Tobiko Cloud](https://tobikodata.com/product.html) to do it for you. If you'd rather handle it yourself, we list recommended and unsupported state engines below.

Recommended state engines for production deployments:

* [Postgres](../integrations/engines/postgres.md)
* [GCP Postgres](../integrations/engines/gcp-postgres.md)

Other state engines with fast and reliable database transactions (less tested than the recommended engines):

* [DuckDB](../integrations/engines/duckdb.md)
    * With the caveat that it's a [single user](https://duckdb.org/docs/connect/concurrency.html#writing-to-duckdb-from-multiple-processes) database so will not scale to production usage
* [MySQL](../integrations/engines/mysql.md)
* [MSSQL](../integrations/engines/mssql.md)

Unsupported state engines, even for development:

* [ClickHouse](../integrations/engines/clickhouse.md)
* [Spark](../integrations/engines/spark.md)
* [StarRocks](../integrations/engines/starrocks.md)
* [Trino](../integrations/engines/trino.md)

This example gateway configuration uses Snowflake for the data warehouse connection and Postgres for the state backend connection:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        connection:
          # snowflake credentials here
          type: snowflake
          user: <username>
          password: <password>
          account: <account>
        state_connection:
          # postgres credentials here
          type: postgres
          host: <host>
          port: <port>
          user: <username>
          password: <password>
          database: <database>
    ```

=== "Python"

    A Postgres connection is specified with a `PostgresConnectionConfig` object.

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        PostgresConnectionConfig,
        SnowflakeConnectionConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                # snowflake credentials here
                connection=SnowflakeConnectionConfig(
                    user=<username>,
                    password=<password>,
                    account=<account>,
                ),
                # postgres credentials here
                state_connection=PostgresConnectionConfig(
                    host=<host>,
                    port=<port>,
                    user=<username>,
                    password=<password>,
                    database=<database>,
                ),
            ),
        }
    )
    ```

#### State schema name

By default, the schema name used to store state tables is `sqlmesh`. This can be changed by providing the `state_schema` config key in the gateway configuration.

Example configuration to store state information in a postgres database's `custom_name` schema:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        state_connection:
          type: postgres
          host: <host>
          port: <port>
          user: <username>
          password: <password>
          database: <database>
        state_schema: custom_name
    ```

=== "Python"


    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        PostgresConnectionConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                state_connection=PostgresConnectionConfig(
                    host=<host>,
                    port=<port>,
                    user=<username>,
                    password=<password>,
                    database=<database>,
                ),
                state_schema="custom_name",
            ),
        }
    )
    ```

This would create all state tables in the schema `custom_name`.

#### Test connection

Configuration for a connection used to run unit tests. An in-memory DuckDB database is used if the `test_connection` key is not specified.

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        test_connection:
          type: duckdb
    ```

=== "Python"

    A DuckDB connection is specified with a `DuckDBConnectionConfig` object. A `DuckDBConnectionConfig` with no arguments specified uses an in-memory DuckDB database.

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        DuckDBConnectionConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                test_connection=DuckDBConnectionConfig(),
            ),
        }
    )
    ```

### Scheduler

Identifies which scheduler backend to use. The scheduler backend is used both for storing metadata and for executing [plans](../concepts/plans.md). By default, the scheduler type is set to `builtin`, which uses the existing SQL engine to store metadata.

These options are in the [scheduler](../reference/configuration.md#scheduler) section of the configuration reference page.

#### Builtin

Example configuration:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        scheduler:
          type: builtin
    ```

=== "Python"

    A built-in scheduler is specified with a `BuiltInSchedulerConfig` object.

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        BuiltInSchedulerConfig,
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                scheduler=BuiltInSchedulerConfig(),
            ),
        }
    )
    ```

No additional configuration options are supported by this scheduler type.


### Gateway/connection defaults

The default gateway and connection keys specify what should happen when gateways or connections are not explicitly specified. These options are in the [gateway/connection defaults](../reference/configuration.md#gatewayconnection-defaults) section of the configuration reference page.

The gateway specified in `default_gateway` is used when a `sqlmesh` command does not explicitly specify a gateway. All SQLMesh CLI commands [accept a gateway option](../reference/cli.md#cli) after `sqlmesh` and before the command name; for example, `sqlmesh --gateway my_gateway plan`. If the option is not specified in a command call, the `default_gateway` is used.

The three default connection types are used when some gateways in the `gateways` configuration dictionaries do not specify every connection type.

#### Default gateway

If a configuration contains multiple gateways, SQLMesh will use the first one in the `gateways` dictionary by default. The `default_gateway` key is used to specify a different gateway name as the SQLMesh default.

Example configuration:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        <gateway specification>
    default_gateway: my_gateway
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                <gateway specification>
            ),
        },
        default_gateway="my_gateway",
    )
    ```

#### Default connections/scheduler

The `default_connection`, `default_test_connection`, and `default_scheduler` keys are used to specify shared defaults across multiple gateways.

For example, you might have a specific connection where your tests should run regardless of which gateway is being used. Instead of duplicating the test connection information in each gateway specification, specify it once in the `default_test_connection` key.

Example configuration specifying a Postgres default connection, in-memory DuckDB default test connection, and builtin default scheduler:

=== "YAML"

    ```yaml linenums="1"
    default_connection:
      type: postgres
      host: <host>
      port: <port>
      user: <username>
      password: <password>
      database: <database>
    default_test_connection:
      type: duckdb
    default_scheduler:
      type: builtin
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        PostgresConnectionConfig,
        DuckDBConnectionConfig,
        BuiltInSchedulerConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        default_connection=PostgresConnectionConfig(
            host=<host>,
            port=<port>,
            user=<username>,
            password=<password>,
            database=<database>,
        ),
        default_test_connection=DuckDBConnectionConfig(),
        default_scheduler=BuiltInSchedulerConfig(),
    )
    ```

### Models

#### Model defaults

The `model_defaults` key is **required** and must contain a value for the `dialect` key. All SQL dialects [supported by the SQLGlot library](https://github.com/tobymao/sqlglot/blob/main/sqlglot/dialects/dialect.py) are allowed. Other values are set automatically unless explicitly overridden in the model definition.

All supported `model_defaults` keys are listed in the [models configuration reference page](../reference/model_configuration.md#model-defaults).

Example configuration:

=== "YAML"

    ```yaml linenums="1"
    model_defaults:
      dialect: snowflake
      owner: jen
      start: 2022-01-01
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig

    config = Config(
        model_defaults=ModelDefaultsConfig(
            dialect="snowflake",
            owner="jen",
            start="2022-01-01",
        ),
    )
    ```

The default model kind is `VIEW` unless overridden with the `kind` key. For more information on model kinds, refer to [model concepts page](../concepts/models/model_kinds.md).

##### Identifier resolution

When a SQL engine receives a query such as `SELECT id FROM "some_table"`, it eventually needs to understand what database objects the identifiers `id` and `"some_table"` correspond to. This process is usually referred to as identifier (or name) resolution.

Different SQL dialects implement different rules when resolving identifiers in queries. For example, certain identifiers may be treated as case-sensitive (e.g. if they're quoted), and a case-insensitive identifier is usually either lowercased or uppercased, before the engine actually looks up what object it corresponds to.

SQLMesh analyzes model queries so that it can extract useful information from them, such as computing Column-Level Lineage. To facilitate this analysis, it _normalizes_ and _quotes_ all identifiers in those queries, [respecting each dialect's resolution rules](https://sqlglot.com/sqlglot/dialects/dialect.html#Dialect.normalize_identifier).

The "normalization strategy", i.e. whether case-insensitive identifiers are lowercased or uppercased, is configurable per dialect. For example, to treat all identifiers as case-sensitive in a BigQuery project, one can do:

=== "YAML"

    ```yaml linenums="1"
    model_defaults:
      dialect: "bigquery,normalization_strategy=case_sensitive"
    ```

This may be useful in cases where the name casing needs to be preserved, since then SQLMesh won't be able to normalize them.

See [here](https://sqlglot.com/sqlglot/dialects/dialect.html#NormalizationStrategy) to learn more about the supported normalization strategies.

##### Gateway-specific model defaults

You can also define gateway specific `model_defaults` in the `gateways` section, which override the global defaults for that gateway.

```yaml linenums="1" hl_lines="6 14"
gateways:
  redshift:
    connection:
      type: redshift
    model_defaults:
      dialect: "snowflake,normalization_strategy=case_insensitive"
  snowflake:
    connection:
      type: snowflake

default_gateway: snowflake

model_defaults:
  dialect: snowflake
  start: 2025-02-05
```

This allows you to tailor the behavior of models for each gateway without affecting the global `model_defaults`.

For example, in some SQL engines identifiers like table and column names are case-sensitive, but they are case-insensitive in other engines. By default, a project that uses both types of engines would need to ensure the models for each engine aligned with the engine's normalization behavior, which makes project maintenance and debugging more challenging.

Gateway-specific `model_defaults` allow you to change how SQLMesh performs identifier normalization *by engine* to align the different engines' behavior.

In the example above, the project's default dialect is `snowflake` (line 14). The `redshift` gateway configuration overrides that global default dialect with `"snowflake,normalization_strategy=case_insensitive"` (line 6).

That value tells SQLMesh that the `redshift` gateway's models will be written in the Snowflake SQL dialect (so need to be transpiled from Snowflake to Redshift), but that the resulting Redshift SQL should treat identifiers as case-insensitive to match Snowflake's behavior.


#### Model Kinds

Model kinds are required in each model file's `MODEL` DDL statement. They may optionally be used to specify a default kind in the model defaults configuration key.

All model kind specification keys are listed in the [models configuration reference page](../reference/model_configuration.md#model-kind-properties).

The `VIEW`, `FULL`, and `EMBEDDED` model kinds are specified by name only, while other models kinds require additional parameters and are provided with an array of parameters:

=== "YAML"

    `FULL` model only requires a name:

    ```sql linenums="1"
    MODEL(
      name docs_example.full_model,
      kind FULL
    );
    ```

    `INCREMENTAL_BY_TIME_RANGE` requires an array specifying the model's `time_column` (which should be in the UTC time zone):

    ```sql linenums="1"
    MODEL(
      name docs_example.incremental_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column model_time_column
      )
    );
    ```

Python model kinds are specified with model kind objects. Python model kind objects have the same arguments as their SQL counterparts, listed in the [models configuration reference page](../reference/model_configuration.md#model-kind-properties).

This example demonstrates how to specify an incremental by time range model kind in Python:

=== "Python"

    ```python linenums="1"
    from sqlmesh import ExecutionContext, model
    from sqlmesh.core.model.kind import ModelKindName

    @model(
        "docs_example.incremental_model",
        kind=dict(
            name=ModelKindName.INCREMENTAL_BY_TIME_RANGE,
            time_column="ds"
        )
    )
    ```

Learn more about specifying Python models at the [Python models concepts page](../concepts/models/python_models.md#model-specification).


#### Model Naming

The `model_naming` configuration controls if model names are inferred based on the project's directory structure. If `model_naming` is not defined or `infer_names` is set to false, the model names must be provided explicitly.

With `infer_names` set to true, model names are inferred based on their path. For example, a model located at `models/catalog/schema/model.sql` would be named `catalog.schema.model`. However, if a name is provided in the model definition, it will take precedence over the inferred name.

Example enabling name inference:

=== "YAML"

    ```yaml linenums="1"
    model_naming:
      infer_names: true
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, NameInferenceConfig

    config = Config(
        model_naming=NameInferenceConfig(
            infer_names=True
        )
    )
    ```

### Before_all and after_all Statements

The `before_all` and `after_all` statements are executed at the start and end, respectively, of the `sqlmesh plan` and `sqlmesh run` commands.

These statements can be defined in the configuration file under the `before_all` and `after_all` keys, either as a list of SQL statements or by using SQLMesh macros:

=== "YAML"

    ```yaml linenums="1"
    before_all:
      - CREATE TABLE IF NOT EXISTS analytics (table VARCHAR, eval_time VARCHAR)
    after_all:
      - "@grant_select_privileges()"
      - "@IF(@this_env = 'prod', @grant_schema_usage())"
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config

    config = Config(
        before_all = [
            "CREATE TABLE IF NOT EXISTS analytics (table VARCHAR, eval_time VARCHAR)"
        ],
        after_all = [
            "@grant_select_privileges()",
            "@IF(@this_env = 'prod', @grant_schema_usage())"
        ],
    )
    ```

#### Examples

These statements allow for actions to be executed before all individual model statements or after all have run, respectively. They can also simplify tasks such as granting privileges.

##### Example: Granting Select Privileges

For example, rather than using an `on_virtual_update` statement in each model to grant privileges on the views of the virtual layer, a single macro can be defined and used at the end of the plan:

```python linenums="1"
from sqlmesh.core.macros import macro

@macro()
def grant_select_privileges(evaluator):
    if evaluator.views:
        return [
            f"GRANT SELECT ON VIEW {view_name} /* sqlglot.meta replace=false */ TO ROLE admin_role;"
            for view_name in evaluator.views
        ]
```

By including the comment `/* sqlglot.meta replace=false */`, you further ensure that the evaluator does not replace the view name with the physical table name during rendering.

##### Example: Granting Schema Privileges

Similarly, you can define a macro to grant schema usage privileges and, as demonstrated in the configuration above, using `this_env` macro conditionally execute it only in the production environment.

```python linenums="1"
from sqlmesh import macro

@macro()
def grant_schema_usage(evaluator):
    if evaluator.this_env == "prod" and evaluator.schemas:
        return [
            f"GRANT USAGE ON SCHEMA {schema} TO admin_role;"
            for schema in evaluator.schemas
        ]
```

As demonstrated in these examples, the `schemas`  and `views` are available within the macro evaluator for macros invoked within the `before_all` and `after_all` statements. Additionally, the macro `this_env` provides access to the current environment name, which can be helpful for more advanced use cases that require fine-grained control over their behaviour.

### Linting

SQLMesh provides a linter that checks for potential issues in your models' code. Enable it and specify which linting rules to apply in the configuration file's `linter` key.

Learn more about linting configuration in the [linting guide](./linter.md).

### Debug mode

To enable debug mode set the `SQLMESH_DEBUG` environment variable to one of the following values: "1", "true", "t", "yes" or "y".

Enabling this mode ensures that full backtraces are printed when using CLI. The default log level is set to `DEBUG` when this mode is enabled.

Example enabling debug mode for the CLI command `sqlmesh plan`:

=== "Bash"

    ```bash
    SQLMESH_DEBUG=1 sqlmesh plan
    ```

=== "MS Powershell"

    ```powershell
    PS> $env:SQLMESH_DEBUG=1
    PS> sqlmesh plan
    ```

=== "MS CMD"

    ```cmd
    C:\> set SQLMESH_DEBUG=1
    C:\> sqlmesh plan
    ```


### Python library dependencies
SQLMesh enables you to write Python models and macros which depend on third-party libraries. To ensure each run / evaluation uses the same version, you can specify versions in a `sqlmesh-requirements.lock` file in the root of your project.

The sqlmesh.lock must be of the format `dep==version`. Only `==` is supported.

For example:

```
numpy==2.1.2
pandas==2.2.3
```

This feature is only available in [Tobiko Cloud](https://tobikodata.com/product.html).

#### Excluding dependencies

You can exclude dependencies by prefixing the dependency with a `^`. For example:

```
^numpy
pandas==2.2.3
```
