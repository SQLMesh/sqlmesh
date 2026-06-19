# Model configuration

This page lists SQLMesh model configuration options and their parameters.

Learn more about specifying SQLMesh model properties in the [model concepts overview page](../concepts/models/overview.md#model-properties).

## General model properties

Configuration options for SQLMesh model properties. Supported by all model kinds other than [`SEED` models](#seed-models).

| Option                | Description                                                                                                                                                                                                                                                                                                                                                 |       Type        | Required |
|-----------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:-----------------:|:--------:|
| `name`                | The model name. Must include at least a qualifying schema (`<schema>.<model>`) and may include a catalog (`<catalog>.<schema>.<model>`). Can be omitted if [infer_names](#model-naming) is set to true.                                                                                                                                                     |        str        |    N     |
| `project`             | The name of the project the model belongs to - used in multi-repo deployments                                                                                                                                                                                                                                                                               |        str        |    N     |
| `kind`                | The model kind ([Additional Details](#model-kind-properties)). (Default: `VIEW`)                                                                                                                                                                                                                                                                            |    str \| dict    |    N     |
| `audits`              | SQLMesh [audits](../concepts/audits.md) that should run against the model's output                                                                                                                                                                                                                                                                          |    array[str]     |    N     |
| `dialect`             | The SQL dialect in which the model's query is written. All SQL dialects [supported by the SQLGlot library](https://github.com/tobymao/sqlglot/blob/main/sqlglot/dialects/dialect.py) are allowed.                                                                                                                                                           |        str        |    N     |
| `owner`               | The owner of a model; may be used for notification purposes                                                                                                                                                                                                                                                                                                 |        str        |    N     |
| `stamp`               | Arbitrary string used to indicate a model's version without changing the model name                                                                                                                                                                                                                                                                         |        str        |    N     |
| `tags`                | Arbitrary strings used to organize or classify a model                                                                                                                                                                                                                                                                                                      |    array[str]     |    N     |
| `cron`                | The cron expression specifying how often the model should be refreshed. (Default: `@daily`)                                                                                                                                                                                                                                                                 |        str        |    N     |
| `interval_unit`       | The temporal granularity of the model's data intervals. Supported values: `year`, `month`, `day`, `hour`, `half_hour`, `quarter_hour`, `five_minute`. (Default: inferred from `cron`)                                                                                                                                                                       |        str        |    N     |
| `start`               | The date/time that determines the earliest date interval that should be processed by a model. Can be a datetime string, epoch time in milliseconds, or a relative datetime such as `1 year ago`. (Default: `yesterday`)                                                                                                                                     |    str \| int     |    N     |
| `end`                 | The date/time that determines the latest date interval that should be processed by a model. Can be a datetime string, epoch time in milliseconds, or a relative datetime such as `1 year ago`.                                                                                                                                                              |    str \| int     |    N     |
| `description`         | Description of the model. Automatically registered in the SQL engine's table COMMENT field or equivalent (if supported by the engine).                                                                                                                                                                                                                      |        str        |    N     |
| `column_descriptions` | A key-value mapping of column names to column comments that will be registered in the SQL engine's table COMMENT field (if supported by the engine). Specified as key-value pairs (`column_name = 'column comment'`). If present, [inline column comments](../concepts/models/overview.md#inline-column-comments) will not be registered in the SQL engine. |       dict        |    N     |
| `grains`              | The column(s) whose combination uniquely identifies each row in the model                                                                                                                                                                                                                                                                                   | str \| array[str] |    N     |
| `references`          | The model column(s) used to join to other models' grains                                                                                                                                                                                                                                                                                                    | str \| array[str] |    N     |
| `depends_on`          | Models on which this model depends, in addition to the ones inferred from the model's query. (Default: dependencies inferred from model code)                                                                                                                                                                                                               |    array[str]     |    N     |
| `table_format`        | The table format that should be used to manage the physical files (eg `iceberg`, `hive`, `delta`); only applicable to engines such as Spark and Athena                                                                                                                                                                                                      |        str        |    N     |
| `storage_format`      | The storage format that should be used to store physical files (eg `parquet`, `orc`); only applicable to engines such as Spark and Athena                                                                                                                                                                                                                   |        str        |    N     |
| `partitioned_by`      | The column(s) and/or column expressions used define a model's partitioning key. Required for the `INCREMENTAL_BY_PARTITION` model kind. Optional for all other model kinds; used to partition the model's physical table in engines that support partitioning.                                                                                              | str \| array[str] |    N     |
| `clustered_by`        | The column(s) and/or column expressions used to cluster the model's physical table; only applicable to engines that support clustering                                                                                                                                                                                                                                                |        str        |    N     |
| `columns`             | The column names and data types returned by the model. Disables [automatic inference of column names and types](../concepts/models/overview.md#conventions) from the SQL query.                                                                                                                                                                             |    array[str]     |    N     |
| `physical_properties` | A key-value mapping of arbitrary properties specific to the target engine that are applied to the model table / view in the physical layer. Specified as key-value pairs (`key = value`). The view/table type (e.g. `TEMPORARY`, `TRANSIENT`) can be added with the `creatable_type` key.                                                                   |       dict        |    N     |
| `virtual_properties`  | A key-value mapping of arbitrary properties specific to the target engine that are applied to the model view in the virtual layer. Specified as key-value pairs (`key = value`). The view type (e.g. `SECURE`) can be added with the `creatable_type` key.                                                                                                  |       dict        |    N     |
| `session_properties`  | A key-value mapping of arbitrary properties specific to the target engine that are applied to the engine session. Specified as key-value pairs (`key = value`).                                                                                                                                                                                             |       dict        |    N     |
| `allow_partials`      | Whether this model can process partial (incomplete) data intervals                                                                                                                                                                                                                                                                                          |       bool        |    N     |
| `enabled`             | Whether the model is enabled. This attribute is `true` by default. Setting it to `false` causes SQLMesh to ignore this model when loading the project.                                                                                                                                                                                                      |       bool        |    N     |
| `gateway`             | Specifies the gateway to use for the execution of this model. When not specified, the default gateway is used.                                                                                                                                                                                                       |       str        |    N     |
| `optimize_query`             | Whether the model's query should be optimized. This attribute is `true` by default. Setting it to `false` causes SQLMesh to disable query canonicalization & simplification. This should be turned off only if the optimized query leads to errors such as surpassing text limit.                                                                                                                                                                                                      |       bool        |    N     |
| `ignored_rules`             |  A list of linter rule names (or "ALL") to be ignored/excluded for this model                                                                                                                                                                                             |       str \| array[str]        |    N     |
| `formatting`             | Whether the model will be formatted. All models are formatted by default. Setting this to `false` causes SQLMesh to ignore this model during `sqlmesh format`.                                                                                                                                                                                                      |       bool        |    N     |

### Model defaults

The SQLMesh project-level configuration must contain the `model_defaults` key and must specify a value for its `dialect` key. Other values are set automatically unless explicitly overridden in the model definition. Learn more about project-level configuration in the [configuration guide](../guides/configuration.md).

In `physical_properties`, `virtual_properties`, and `session_properties`, when both project-level and model-specific properties are defined, they are merged, with model-level properties taking precedence. To unset a project-wide property for a specific model, set it to `None` in the `MODEL`'s DDL properties or within the `@model` decorator for Python models.

For example, with the following `model_defaults` configuration:

=== "YAML"

    ```yaml linenums="1"
    model_defaults:
      dialect: snowflake
      start: 2022-01-01
      physical_properties:
        partition_expiration_days: 7
        require_partition_filter: True
        project_level_property: "value"
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig

    config = Config(
      model_defaults=ModelDefaultsConfig(
        dialect="snowflake",
        start="2022-01-01",
        physical_properties={
          "partition_expiration_days": 7,
          "require_partition_filter": True,
          "project_level_property": "value"
        },
      ),
    )
    ```

To override `partition_expiration_days`, add a new `creatable_type` property and unset `project_level_property`, you can define the model as follows:

=== "SQL"

    ```sql linenums="1"
    MODEL (
      ...,
      physical_properties (
        partition_expiration_days = 14,
        creatable_type = TRANSIENT,
        project_level_property = None,
      )
    );
    ```

=== "Python"

    ```python linenums="1"
    @model(
      ...,
      physical_properties={
        "partition_expiration_days": 14,
        "creatable_type": "TRANSIENT",
        "project_level_property": None
      },
    )
    ```

You can also use the `@model_kind_name` variable to fine-tune control over `physical_properties` in `model_defaults`. This holds the current model's kind name and is useful for conditionally assigning a property. For example, to disable `creatable_type` for your project's `VIEW` kind models:

=== "YAML"

    ```yaml linenums="1"
    model_defaults:
      dialect: snowflake
      start: 2022-01-01
      physical_properties:
        creatable_type: "@IF(@model_kind_name != 'VIEW', 'TRANSIENT', NULL)"
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig

    config = Config(
      model_defaults=ModelDefaultsConfig(
        dialect="snowflake",
        start="2022-01-01",
        physical_properties={
          "creatable_type": "@IF(@model_kind_name != 'VIEW', 'TRANSIENT', NULL)",
        },
      ),
    )
    ```

You can aso define `pre_statements`, `post_statements` and `on_virtual_update` statements at the project level that will be applied to all models. These default statements are merged with any model-specific statements, with default statements executing first, followed by model-specific statements.

=== "YAML"

    ```yaml linenums="1"
    model_defaults:
      dialect: duckdb
      pre_statements:
        - "SET timeout = 300000"
      post_statements:
        - "@IF(@runtime_stage = 'evaluating', ANALYZE @this_model)"
      on_virtual_update:
        - "GRANT SELECT ON @this_model TO ROLE analyst_role"
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import Config, ModelDefaultsConfig

    config = Config(
      model_defaults=ModelDefaultsConfig(
        dialect="duckdb",
        pre_statements=[
          "SET query_timeout = 300000",
        ],
        post_statements=[
          "@IF(@runtime_stage = 'evaluating', ANALYZE @this_model)",
        ],
        on_virtual_update=[
          "GRANT SELECT ON @this_model TO ROLE analyst_role",
        ],
      ),
    )
    ```


The SQLMesh project-level `model_defaults` key supports the following options, described in the [general model properties](#general-model-properties) table above:

- kind
- dialect
- cron
- cron_tz
- owner
- start
- table_format
- storage_format
- physical_properties
- virtual_properties
- session_properties (on per key basis)
- on_destructive_change (described [below](#incremental-models))
- on_additive_change (described [below](#incremental-models))
- audits (described [here](../concepts/audits.md#generic-audits))
- optimize_query
- allow_partials
- enabled
- interval_unit
- pre_statements (described [here](../concepts/models/sql_models.md#pre--and-post-statements))
- post_statements (described [here](../concepts/models/sql_models.md#pre--and-post-statements))
- on_virtual_update (described [here](../concepts/models/sql_models.md#on-virtual-update-statements))


### Model Naming

Configuration option for name inference. Learn more in the [model naming guide](../guides/configuration.md#model-naming).

| Option        | Description                                                                                    | Type | Required |
|---------------|------------------------------------------------------------------------------------------------|:----:|:--------:|
| `infer_names` | Whether to automatically infer model names based on the directory structure (Default: `False`) | bool |    N     |


## Model kind properties

Configuration options for kind-specific SQLMesh model properties, in addition to the [general model properties](#general-model-properties) listed above.

Learn more about model kinds at the [model kind concepts page](../concepts/models/model_kinds.md). Learn more about specifying model kind in Python models at the [Python models concepts page](../concepts/models/python_models.md#model-specification).

### `VIEW` models

Configuration options for models of the [`VIEW` kind](../concepts/models/model_kinds.md#view) (in addition to [general model properties](#general-model-properties)).

| Option         | Description                                                                                          | Type | Required |
|----------------|------------------------------------------------------------------------------------------------------|:----:|:--------:|
| `materialized` | Whether views should be materialized (for engines supporting materialized views). (Default: `False`) | bool |    N     |

Python model kind `name` enum value: [ModelKindName.VIEW](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName)

### `FULL` models

The [`FULL` model kind](../concepts/models/model_kinds.md#full) does not support any configuration options other than the [general model properties listed above](#general-model-properties).

Python model kind `name` enum value: [ModelKindName.FULL](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName)

### Incremental models

Configuration options for all incremental models (in addition to [general model properties](#general-model-properties)).

| Option                  | Description                                                                                                                                                                                                                                                                                                                                              | Type | Required |
|-------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:----:|:--------:|
| `forward_only`          | Whether the model's changes should always be classified as [forward-only](../concepts/plans.md#forward-only-change). (Default: `False`)                                                                                                                                                                                                                  | bool |    N     |
| `on_destructive_change` | What should happen when a change to a [forward-only model](../guides/incremental_time.md#forward-only-models) or incremental model in a [forward-only plan](../concepts/plans.md#forward-only-plans) causes a destructive modification to the model schema. Valid values: `allow`, `warn`, `error`, `ignore`. (Default: `error`)                         | str  |    N     |
| `on_additive_change`    | What should happen when a change to a [forward-only model](../guides/incremental_time.md#forward-only-models) or incremental model in a [forward-only plan](../concepts/plans.md#forward-only-plans) causes an additive modification to the model schema (like adding new columns). Valid values: `allow`, `warn`, `error`, `ignore`. (Default: `allow`) | str  |    N     |
| `disable_restatement`   | Whether [restatements](../concepts/plans.md#restatement-plans) should be disabled for the model. (Default: `False`)                                                                                                                                                                                                                                      | bool |    N     |

#### Incremental by time range

Configuration options for [`INCREMENTAL_BY_TIME_RANGE` models](../concepts/models/model_kinds.md#incremental_by_time_range) (in addition to [general model properties](#general-model-properties) and [incremental model properties](#incremental-models)).

| Option              | Description                                                                                                                                                                                                                                                                                                                      | Type | Required |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--: | :------: |
| `time_column`       | The model column containing each row's timestamp. Should be UTC time zone.                                                                                                                                                                                                                                                       | str  |    Y     |
| `format`            | Argument to `time_column`. Format of the time column's data. (Default: `%Y-%m-%d`)                                                                                                                                                                                                                                               | str  |    N     |
| `batch_size`        | The maximum number of intervals that can be evaluated in a single backfill task. If this is `None`, all intervals will be processed as part of a single task. If this is set, a model's backfill will be chunked such that each individual task only contains jobs with the maximum of `batch_size` intervals. (Default: `None`) | int  |    N     |
| `batch_concurrency` | The maximum number of batches that can run concurrently for this model. (Default: the number of concurrent tasks set in the connection settings)                                                                                                                                                                                 | int  |    N     |
| `lookback`          | The number of `interval_unit`s prior to the current interval that should be processed - [learn more](../concepts/models/overview.md#lookback). (Default: `0`)                                                                                                                                                                                                        | int  |    N     |

Python model kind `name` enum value: [ModelKindName.INCREMENTAL_BY_TIME_RANGE](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName)

#### Incremental by unique key

Configuration options for [`INCREMENTAL_BY_UNIQUE_KEY` models](../concepts/models/model_kinds.md#incremental_by_unique_key) (in addition to [general model properties](#general-model-properties) and [incremental model properties](#incremental-models)). Batch concurrency cannot be set for incremental by unique key models because they cannot safely be run in parallel.

| Option         | Description                                                                                                                                                                                                                                                                                                                      | Type              | Required |
|----------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------|----------|
| `unique_key`   | The model column(s) containing each row's unique key                                                                                                                                                                                                                                                                             | str \| array[str] | Y        |
| `when_matched` | SQL logic used to update columns when a match occurs - only available on engines that support `MERGE`. (Default: update all columns)                                                                                                                                                                                             | str               | N        |
| `merge_filter` | A single or a conjunction of predicates used to filter data in the ON clause of a MERGE operation - only available on engines that support `MERGE`                                                                                                                                                                               | str               | N        |
| `batch_size`   | The maximum number of intervals that can be evaluated in a single backfill task. If this is `None`, all intervals will be processed as part of a single task. If this is set, a model's backfill will be chunked such that each individual task only contains jobs with the maximum of `batch_size` intervals. (Default: `None`) | int               | N        |
| `lookback`     | The number of time unit intervals prior to the current interval that should be processed. (Default: `0`)                                                                                                                                                                                                                         | int               | N        |

Python model kind `name` enum value: [ModelKindName.INCREMENTAL_BY_UNIQUE_KEY](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName)

#### Incremental by partition

The [`INCREMENTAL_BY_PARTITION` models](../concepts/models/model_kinds.md#incremental_by_partition) kind does not support any configuration options other than the [general model properties](#general-model-properties) and [incremental model properties](#incremental-models).

Python model kind `name` enum value: [ModelKindName.INCREMENTAL_BY_PARTITION](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName)

#### SCD Type 2 models

Configuration options for [`SCD_TYPE_2` models](../concepts/models/model_kinds.md#scd-type-2) (in addition to [general model properties](#general-model-properties) and [incremental model properties](#incremental-models)).

| Option                    | Description                                                                                                                                                                                 |    Type    | Required |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--------: | :------: |
| `unique_key`              | The model column(s) containing each row's unique key                                                                                                                                        | array[str] |    Y     |
| `valid_from_name`         | The model column containing each row's valid from date. (Default: `valid_from`)                                                                                                             |    str     |    N     |
| `valid_to_name`           | The model column containing each row's valid to date. (Default: `valid_to`)                                                                                                                 |    str     |    N     |
| `invalidate_hard_deletes` | If set to true, when a record is missing from the source table it will be marked as invalid - see [here](../concepts/models/model_kinds.md#deletes) for more information. (Default: `False`) |    bool    |    N     |

##### SCD Type 2 By Time

Configuration options for [`SCD_TYPE_2_BY_TIME` models](../concepts/models/model_kinds.md#scd-type-2) (in addition to [general model properties](#general-model-properties), [incremental model properties](#incremental-models), and [SCD Type 2 properties](#scd-type-2-models)).

| Option                     | Description                                                                                                                                                                      | Type | Required |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--: | :------: |
| `updated_at_name`          | The model column containing each row's updated at date. (Default: `updated_at`)                                                                                                  | str  |    N     |
| `updated_at_as_valid_from` | By default, for new rows the `valid_from` column is set to 1970-01-01 00:00:00. This sets `valid_from` to the value of `updated_at` when the row is inserted. (Default: `False`) | bool |    N     |

Python model kind `name` enum value: [ModelKindName.SCD_TYPE_2_BY_TIME](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName)

##### SCD Type 2 By Column

Configuration options for [`SCD_TYPE_2_BY_COLUMN` models](../concepts/models/model_kinds.md#scd-type-2) (in addition to [general model properties](#general-model-properties), [incremental model properties](#incremental-models), and [SCD Type 2 properties](#scd-type-2-models)).

| Option                         | Description                                                                                                                                                                 |       Type        | Required |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :---------------: | :------: |
| `columns`                      | Columns whose changed data values indicate a data update (instead of an `updated_at` column). `*` to represent that all columns should be checked.                          | str \| array[str] |    Y     |
| `execution_time_as_valid_from` | By default, for new rows `valid_from` is set to 1970-01-01 00:00:00. This changes the behavior to set it to the execution_time of when the pipeline ran. (Default: `False`) |       bool        |    N     |

Python model kind `name` enum value: [ModelKindName.SCD_TYPE_2_BY_COLUMN](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName)

### `SEED` models

Configuration options for [`SEED` models](../concepts/models/model_kinds.md#seed). `SEED` models do not support all the general properties supported by other models; they only support the properties listed in this table.

Top-level options inside the MODEL DDL:

| Option        | Description                                                                                                                                                                                                                |    Type    | Required |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--------: | :------: |
| `name`        | The model name. Must include at least a qualifying schema (`<schema>.<model>`) and may include a catalog (`<catalog>.<schema>.<model>`). Can be omitted if [infer_names](#model-naming) is set to true.                                                                                |    str     |    N     |
| `kind`        | The model kind. Must be `SEED`.                                                                                                                                                                                            |    str     |    Y     |
| `columns`     | The column names and data types in the CSV file. Disables automatic inference of column names and types by the pandas CSV reader. NOTE: order of columns overrides the order specified in the CSV header row (if present). | array[str] |    N     |
| `audits`      | SQLMesh [audits](../concepts/audits.md) that should run against the model's output                                                                                                                                         | array[str] |    N     |
| `owner`       | The owner of a model; may be used for notification purposes                                                                                                                                                                |    str     |    N     |
| `stamp`       | Arbitrary string used to indicate a model's version without changing the model name                                                                                                                                        |    str     |    N     |
| `tags`        | Arbitrary strings used to organize or classify a model                                                                                                                                                                     | array[str] |    N     |
| `description` | Description of the model. Automatically registered in the SQL engine's table COMMENT field or equivalent (if supported by the engine).                                                                                     |    str     |    N     |

Options specified within the top-level `kind` property:

| Option         | Description                                                                                                                                                                             | Type | Required |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- | -------- |
| `path`         | Path to seed CSV file.                                                                                                                                                                  | str  | Y        |
| `batch_size`   | The maximum number of CSV rows ingested in each batch. All rows ingested in one batch if not specified.                                                                                 | int  | N        |
| `csv_settings` | Pandas CSV reader settings (overrides [default values](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html)). Specified as key-value pairs (`key = value`). | dict | N        |

<a id="csv_settings"></a>
Options specified within the `kind` property's `csv_settings` property (overrides [default Pandas CSV reader settings](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html)):

| Option             | Description                                                                                                                                                                                                                                                                         | Type | Required |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- | -------- |
| `delimiter`        | Character or regex pattern to treat as the delimiter. More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html).                                                                                              | str  | N        |
| `quotechar`        | Character used to denote the start and end of a quoted item. More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html).                                                                                       | str  | N        |
| `doublequote`      | When quotechar is specified, indicate whether or not to interpret two consecutive quotechar elements INSIDE a field as a single quotechar element. More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html). | bool | N        |
| `escapechar`       | Character used to escape other characters. More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html).                                                                                                         | str  | N        |
| `skipinitialspace` | Skip spaces after delimiter. More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html).                                                                                                                       | bool | N        |
| `lineterminator`   | Character used to denote a line break. More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html).                                                                                                             | str  | N        |
| `encoding`         | Encoding to use for UTF when reading/writing (ex. 'utf-8'). More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html).                                                                                        | str  | N        |
| `na_values` | An array of values that should be recognized as NA/NaN. In order to specify such an array per column, a mapping in the form of `(col1 = (v1, v2, ...), col2 = ...)` can be passed instead. These values can be integers, strings, booleans or NULL, and they are converted to their corresponding Python values. More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html).                                                                                        | array[value] \| array[array[key = value]]  | N        |
| `keep_default_na` | Whether or not to include the default NaN values when parsing the data. More information at the [Pandas documentation](https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html).                                                                                                                       | bool | N        |


Python model kind `name` enum value: [ModelKindName.SEED](https://sqlmesh.readthedocs.io/en/stable/_readthedocs/html/sqlmesh/core/model/kind.html#ModelKindName)
