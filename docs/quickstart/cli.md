# CLI

In this quickstart, you'll use the SQLMesh command line interface (CLI) to get up and running with SQLMesh's scaffold generator.

It will create an example project that runs locally on your computer using [DuckDB](https://duckdb.org/) as an embedded SQL engine.

Before beginning, ensure that you meet all the [prerequisites](../prerequisites.md) for using SQLMesh.

??? info "Learn more about the quickstart project structure"
    This project demonstrates key SQLMesh features by walking through the SQLMesh workflow on a simple data pipeline. This section describes the project structure and the SQLMesh concepts you will encounter as you work through it.

    The project contains three models with a CSV file as the only data source:

    ```
    ┌─────────────┐
    │seed_data.csv│
    └────────────┬┘
                 │
                ┌▼─────────────┐
                │seed_model.sql│
                └─────────────┬┘
                              │
                             ┌▼────────────────────┐
                             │incremental_model.sql│
                             └────────────────────┬┘
                                                  │
                                                 ┌▼─────────────┐
                                                 │full_model.sql│
                                                 └──────────────┘
    ```

    Although the project is simple, it touches on all the primary concepts needed to use SQLMesh productively.

## 1. Create the SQLMesh project
First, create a project directory and navigate to it:

```bash
mkdir sqlmesh-example
```
```bash
cd sqlmesh-example
```

If using a Python virtual environment, ensure it's activated first by running the `source .venv/bin/activate` command from the folder used during [installation](../installation.md).

### 1.1 Initialize the project

SQLMesh includes a scaffold generator to initialize a new SQLMesh project.

The scaffold generator will ask you some questions and create a SQLMesh configuration file based on your responses.

Depending on your answers, it will also create multiple files for the SQLmesh example project used in this quickstart.

Start the scaffold generator by executing the `sqlmesh init` command:

```bash
sqlmesh init
```

??? info "Skip the questions"

    If you don't want to use the interactive scaffold generator, you can initialize your project with arguments to the [`sqlmesh init` command](../reference/cli.md#init).

    The only required argument is `engine`, which specifies the SQL engine your project will use. Specify one of the engine `type`s in the [list of supported engines](../integrations/overview.md#execution-engines).

    In this example, we specify the `duckdb` engine:

    ```bash
    sqlmesh init duckdb
    ```

    The scaffold will include a SQLMesh configuration file and example project directories and files. You're now ready to continue the quickstart [below](#2-create-a-prod-environment).

#### Project type

The first question asks about the type of project you want to create. Enter the number corresponding to the type of project you want to create and press `Enter`.

``` bash
──────────────────────────────
Welcome to SQLMesh!
──────────────────────────────

What type of project do you want to set up?

    [1] DEFAULT - Create SQLMesh example project models and files
    [2] dbt     - You have an existing dbt project and want to run it with SQLMesh
    [3] EMPTY   - Create a SQLMesh configuration file and project directories only

Enter a number: 1
```

For this quickstart, choose the `DEFAULT` option `1` so the example project files are included in the project directories.

#### SQL engine

The second question asks which SQL engine your project will use. SQLMesh will include that engine's connection settings in the configuration file, which you will fill in later to connect your project to the engine.

For this quickstart, choose the `DuckDB` option `1` so we can run the example project with the built-in DuckDB engine that doesn't need additional configuration.

``` bash
Choose your SQL engine:

    [1]  DuckDB
    [2]  Snowflake
    [3]  Databricks
    [4]  BigQuery
    [5]  MotherDuck
    [6]  ClickHouse
    [7]  Redshift
    [8]  Spark
    [9]  Trino
    [10] Azure SQL
    [11] MSSQL
    [12] Postgres
    [13] GCP Postgres
    [14] MySQL
    [15] Athena
    [16] RisingWave

Enter a number: 1
```

#### CLI mode

SQLMesh's core commands have multiple options that alter their behavior. Some of those options streamline the SQLMesh `plan` workflow and CLI output.

If you prefer a streamlined workflow (no prompts, no file diff previews, auto-apply changes), choose the `FLOW` CLI mode to automatically include those options in your project configuration file.

If you prefer to see all the output SQLMesh provides, choose `DEFAULT` mode, which we will use in this quickstart:

``` bash
Choose your SQLMesh CLI experience:

    [1] DEFAULT - See and control every detail
    [2] FLOW    - Automatically run changes and show summary output

Enter a number: 1
```

#### Ready to go

Your project is now ready to go, and SQLMesh displays a message with some good next steps.

If you chose the DuckDB engine, you're ready to move forward and run the example project with DuckDB.

If you chose a different engine, add your engine's connection information to the `config.yaml` file before you run any additional SQLMesh commands.

``` bash
Your SQLMesh project is ready!

Next steps:
- Update your gateway connection settings (e.g., username/password) in the project configuration file:
    /sqlmesh-example/config.yaml
- Run command in CLI: sqlmesh plan
- (Optional) Explain a plan: sqlmesh plan --explain

Quickstart guide:
https://sqlmesh.readthedocs.io/en/stable/quickstart/cli/

Need help?
- Docs:   https://sqlmesh.readthedocs.io
- Slack:  https://www.tobikodata.com/slack
- GitHub: https://github.com/SQLMesh/sqlmesh/issues
```

??? info "Learn more about the project's configuration: `config.yaml`"
    SQLMesh project-level configuration parameters are specified in the `config.yaml` file in the project directory.

    This example project uses the embedded DuckDB SQL engine, so its configuration specifies `duckdb` as the gateway's connection type. All available configuration settings are included in the file, with optional settings set to their default value and commented out.

    SQLMesh requires a default model SQL dialect. SQLMesh automatically specifies the SQL dialect for your project's SQL engine, which it places in the config `model_defaults` `dialect` key. In this example, we specified the DuckDB engine, so `duckdb` is the default SQL dialect:

    ```yaml linenums="1"
    # --- Gateway Connection ---
    gateways:
      duckdb:
        connection:
          # For more information on configuring the connection to your execution engine, visit:
          # https://sqlmesh.readthedocs.io/en/stable/reference/configuration/#connection
          # https://sqlmesh.readthedocs.io/en/stable/integrations/engines/duckdb/#connection-options
          #
          type: duckdb               # <-- DuckDB engine
          database: db.db
          # concurrent_tasks: 1
          # register_comments: True  # <-- Optional setting `register_comments` has a default value of True
          # pre_ping: False
          # pretty_sql: False
          # catalogs:                # <-- Optional setting `catalogs` has no default value
          # extensions:
          # connector_config:
          # secrets:
          # token:

    default_gateway: duckdb

    # --- Model Defaults ---
    # https://sqlmesh.readthedocs.io/en/stable/reference/model_configuration/#model-defaults

    model_defaults:
      dialect: duckdb                # <-- Models written in DuckDB SQL dialect by default
      start: 2025-06-12 # Start date for backfill history
      cron: '@daily'    # Run models daily at 12am UTC (can override per model)

    # --- Linting Rules ---
    # Enforce standards for your team
    # https://sqlmesh.readthedocs.io/en/stable/guides/linter/

    linter:
      enabled: true
      rules:
        - ambiguousorinvalidcolumn
        - invalidselectstarexpansion
    ```

    Learn more about SQLMesh project configuration [here](../reference/configuration.md).

The scaffold generator creates multiple directories where SQLMesh project files are stored and multiple files that constitute the example project (e.g., SQL models).

??? info "Learn more about the project directories and files"
    SQLMesh uses a scaffold generator to initiate a new project. The generator will create multiple sub-directories and files for organizing your SQLMesh project code.

    The scaffold generator will create the following configuration file and directories:

    - config.yaml
        - The file for project configuration. More info about configuration [here](../guides/configuration.md).
    - ./models
        - SQL and Python models. More info about models [here](../concepts/models/overview.md).
    - ./seeds
        - Seed files. More info about seeds [here](../concepts/models/seed_models.md).
    - ./audits
        - Shared audit files. More info about audits [here](../concepts/audits.md).
    - ./tests
        - Unit test files. More info about tests [here](../concepts/tests.md).
    - ./macros
        - Macro files. More info about macros [here](../concepts/macros/overview.md).

    It will also create the files needed for this quickstart example:

    - ./models
        - full_model.sql
        - incremental_model.sql
        - seed_model.sql
    - ./seeds
        - seed_data.csv
    - ./audits
        - assert_positive_order_ids.sql
    - ./tests
        - test_full_model.yaml

Finally, the scaffold generator creates data for the example project to use.

??? info "Learn more about the project's data"
    The data used in this example project is contained in the `seed_data.csv` file in the `/seeds` project directory. The data reflects sales of 3 items over 7 days in January 2020.

    The file contains three columns, `id`, `item_id`, and `event_date`, which correspond to each row's unique ID, the sold item's ID number, and the date the item was sold, respectively.

    This is the complete dataset:

    | id | item_id | event_date |
    | -- | ------- | ---------- |
    | 1  | 2       | 2020-01-01 |
    | 2  | 1       | 2020-01-01 |
    | 3  | 3       | 2020-01-03 |
    | 4  | 1       | 2020-01-04 |
    | 5  | 1       | 2020-01-05 |
    | 6  | 1       | 2020-01-06 |
    | 7  | 1       | 2020-01-07 |

## 2. Create a prod environment

SQLMesh's key actions are creating and applying *plans* to *environments*. At this point, the only environment is the empty `prod` environment.

??? info "Learn more about SQLMesh plans and environments"

    SQLMesh's key actions are creating and applying *plans* to *environments*.

    A [SQLMesh environment](../concepts/environments.md) is an isolated namespace containing models and the data they generated.

    The most important environment is `prod` ("production"), which consists of the databases behind the applications your business uses to operate each day. Environments other than `prod` provide a place where you can test and preview changes to model code before they go live and affect business operations.

    A [SQLMesh plan](../concepts/plans.md) contains a comparison of one environment to another and the set of changes needed to bring them into alignment.

    For example, if a new SQL model was added, tested, and run in the `dev` environment, it would need to be added and run in the `prod` environment to bring them into alignment. SQLMesh identifies all such changes and classifies them as either breaking or non-breaking.

    Breaking changes are those that invalidate data already existing in an environment. For example, if a `WHERE` clause was added to a model in the `dev` environment, existing data created by that model in the `prod` environment are now invalid because they may contain rows that would be filtered out by the new `WHERE` clause.

    Other changes, like adding a new column to a model in `dev`, are non-breaking because all the existing data in `prod` are still valid to use - only new data must be added to align the environments.

    After SQLMesh creates a plan, it summarizes the breaking and non-breaking changes so you can understand what will happen if you apply the plan. It will prompt you to "backfill" data to apply the plan. (In this context, backfill is a generic term for updating or adding to a table's data, including an initial load or full refresh.)

??? info "Learn more about a plan's actions: `sqlmesh plan --explain`"

    Before applying a plan, you can view a detailed description of the actions it will take by passing the explain flag in your `sqlmesh plan` command:

    ```bash
    sqlmesh plan --explain
    ```

    Passing the explain flag for the quickstart example project above adds the following information to the output:

    ```bash
    Explained plan
    ├── Validate SQL and create physical layer tables and views if they do not exist
    │   ├── sqlmesh_example.seed_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__seed_model__2185867172
    │   │   ├── Dry run model query without inserting results
    │   │   └── Create table if it doesn't exist
    │   ├── sqlmesh_example.full_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__full_model__2278521865
    │   │   ├── Dry run model query without inserting results
    │   │   └── Create table if it doesn't exist
    │   └── sqlmesh_example.incremental_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__incremental_model__1880815781
    │       ├── Dry run model query without inserting results
    │       └── Create table if it doesn't exist
    ├── Backfill models by running their queries and run standalone audits
    │   ├── sqlmesh_example.seed_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__seed_model__2185867172
    │   │   └── Fully refresh table
    │   ├── sqlmesh_example.full_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__full_model__2278521865
    │   │   ├── Fully refresh table
    │   │   └── Run 'assert_positive_order_ids' audit
    │   └── sqlmesh_example.incremental_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__incremental_model__1880815781
    │       └── Fully refresh table
    └── Update the virtual layer for environment 'prod'
        └── Create or update views in the virtual layer to point at new physical tables and views
            ├── sqlmesh_example.full_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__full_model__2278521865
            ├── sqlmesh_example.seed_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__seed_model__2185867172
            └── sqlmesh_example.incremental_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__incremental_model__1880815781
    ```

    The explanation has three top-level sections, corresponding to the three types of actions a plan takes:

      - Validate SQL and create physical layer tables and views if they do not exist
      - Backfill models by running their queries and run standalone audits
      - Update the virtual layer for environment 'prod'

    Each section lists the affected models and provides more information about what will occur. For example, the first model in the first section is:

    ```bash
    ├── sqlmesh_example.seed_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__seed_model__2185867172
    │   ├── Dry run model query without inserting results
    │   └── Create table if it doesn't exist
    ```

    The first line shows the model name `sqlmesh_example.seed_model` and the physical layer table SQLMesh will create to store its data: `db.sqlmesh__sqlmesh_example.sqlmesh_example__seed_model__2185867172`. The second and third lines tell us that in this step SQLMesh will dry-run the model query and create the physical layer table if it doesn't exist.

    The second section describes what will occur during the backfill step. The second model in this section is:

    ```bash
    ├── sqlmesh_example.full_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__full_model__2278521865
    │   ├── Fully refresh table
    │   └── Run 'assert_positive_order_ids' audit
    ```

    The first line shows the model name `sqlmesh_example.full_model` and the physical layer table SQLMesh will insert the model's data into: `db.sqlmesh__sqlmesh_example.sqlmesh_example__full_model__2278521865`. The second and third lines tell us that the backfill action will fully refresh the model's physical table and run the `assert_positive_order_ids` audit.

    The final section describes SQLMesh's action during the virtual layer update step. The first model in this section is:

    ```bash
    └── Create or update views in the virtual layer to point at new physical tables and views
        ├── sqlmesh_example.full_model -> db.sqlmesh__sqlmesh_example.sqlmesh_example__full_model__2278521865
    ```

    The virtual layer step will update the `sqlmesh_example.full_model` virtual layer view to `SELECT * FROM` the physical table `db.sqlmesh__sqlmesh_example.sqlmesh_example__full_model__2278521865`.

The first SQLMesh plan must execute every model to populate the production environment. Running `sqlmesh plan` will generate the plan and the following output:

```bash linenums="1"
$ sqlmesh plan
======================================================================
Successfully Ran 1 tests against duckdb in 0.1 seconds.
----------------------------------------------------------------------

`prod` environment will be initialized

Models:
└── Added:
    ├── sqlmesh_example.full_model
    ├── sqlmesh_example.incremental_model
    └── sqlmesh_example.seed_model
Models needing backfill:
├── sqlmesh_example.full_model: [full refresh]
├── sqlmesh_example.incremental_model: [2020-01-01 - 2025-06-22]
└── sqlmesh_example.seed_model: [full refresh]
Apply - Backfill Tables [y/n]:
```

Line 3 of the output notes that `sqlmesh plan` successfully executed the project's test `tests/test_full_model.yaml` with duckdb.

Line 6 describes what environments the plan will affect when applied - a new `prod` environment in this case.

Lines 8-12 of the output show that SQLMesh detected three new models relative to the current empty environment.

Lines 13-16 list each model that will be executed by the plan, along with the date intervals or refresh types. For both `full_model` and `seed_model`, it shows `[full refresh]`, while for `incremental_model` it shows a specific date range `[2020-01-01 - 2025-06-22]`. The incremental model date range begins from 2020-01-01 because its definition specifies a model start date of `2020-01-01`.

??? info "Learn more about the project's models"

    A plan's actions are determined by the [kinds](../concepts/models/model_kinds.md) of models the project uses. This example project uses three model kinds:

    1. [`SEED` models](../concepts/models/model_kinds.md#seed) read data from CSV files stored in the SQLMesh project directory.
    2. [`FULL` models](../concepts/models/model_kinds.md#full) fully refresh (rewrite) the data associated with the model every time the model is run.
    3. [`INCREMENTAL_BY_TIME_RANGE` models](../concepts/models/model_kinds.md#incremental_by_time_range) use a date/time data column to track which time intervals are affected by a plan and process only the affected intervals when a model is run.

    We now briefly review each model in the project.

    The first model is a `SEED` model that imports `seed_data.csv`. This model consists of only a `MODEL` statement because `SEED` models do not query a database.

    In addition to specifying the model name and CSV path relative to the model file, it includes the column names and data types of the columns in the CSV. It also sets the `grain` of the model to the columns that collectively form the model's unique identifier, `id` and `event_date`.

    ```sql linenums="1"
    MODEL (
      name sqlmesh_example.seed_model,
      kind SEED (
        path '../seeds/seed_data.csv'
      ),
      columns (
        id INTEGER,
        item_id INTEGER,
        event_date DATE
      ),
      grain (id, event_date)
    );
    ```

    The second model is an `INCREMENTAL_BY_TIME_RANGE` model that includes both a `MODEL` statement and a SQL query selecting from the first seed model.

    The `MODEL` statement's `kind` property includes the required specification of the data column containing each record's timestamp. It also includes the optional `start` property specifying the earliest date/time for which the model should process data and the `cron` property specifying that the model should run daily. It sets the model's grain to columns `id` and `event_date`.

    The SQL query includes a `WHERE` clause that SQLMesh uses to filter the data to a specific date/time interval when loading data incrementally:

    ```sql linenums="1"
    MODEL (
      name sqlmesh_example.incremental_model,
      kind INCREMENTAL_BY_TIME_RANGE (
        time_column event_date
      ),
      start '2020-01-01',
      cron '@daily',
      grain (id, event_date)
    );

    SELECT
      id,
      item_id,
      event_date,
    FROM
      sqlmesh_example.seed_model
    WHERE
      event_date between @start_date and @end_date
    ```

    The final model in the project is a `FULL` model. In addition to properties used in the other models, its `MODEL` statement includes the [`audits`](../concepts/audits.md) property. The project includes a custom `assert_positive_order_ids` audit in the project `audits` directory; it verifies that all `item_id` values are positive numbers. It will be run every time the model is executed.

    ```sql linenums="1"
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
    ```

Line 18 asks you whether to proceed with executing the model backfills described in lines 13-16. Enter `y` and press `Enter`, and SQLMesh will execute the models and return this output:

```bash linenums="1"
Apply - Backfill Tables [y/n]: y

Updating physical layer ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 3/3 • 0:00:00

✔ Physical layer updated

[1/1] sqlmesh_example.seed_model          [insert seed file]                 0.01s
[1/1] sqlmesh_example.incremental_model   [insert 2020-01-01 - 2025-06-22]   0.01s
[1/1] sqlmesh_example.full_model          [full refresh, audits ✔1]          0.01s
Executing model batches ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 3/3 • 0:00:00

✔ Model batches executed

Updating virtual layer  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 3/3 • 0:00:00

✔ Virtual layer updated
```

SQLMesh performs three actions when applying the plan:

- Creating and storing new versions of the models
- Evaluating/running the models
- Virtually updating the plan's target environment

Lines 2-4 show the progress and completion of the first step - updating the physical layer (creating new model versions).

Lines 6-11 show the execution of each model with their specific operations and timing. Line 6 shows the seed model being inserted, line 8 shows the incremental model being inserted for the specified date range, and line 10 shows the full model being processed with its audit check passing.

Lines 12-14 show the progress and completion of the second step - executing model batches.

Lines 16-18 show the progress and completion of the final step - virtually updating the plan's target environment, which makes the data available for querying.

Let's take a quick look at the project's DuckDB database file to see the objects SQLMesh created. First, we open the built-in DuckDB CLI tool with the `duckdb db.db` command, then run our two queries.

Our first query shows the three physical tables SQLMesh created in the `sqlmesh__sqlmesh_example` schema (one table for each model):

![Example project physical layer tables in the DuckDB CLI](./cli/cli-quickstart_duckdb-tables.png)

Our second query shows that in the `sqlmesh` schema SQLMesh created three virtual layer views that read from the three physical tables:

![Example project virtual layer views in the DuckDB CLI](./cli/cli-quickstart_duckdb-views.png)

You've now created a new production environment with all of history backfilled!

## 3. Update a model

Now that we have populated the `prod` environment, let's modify one of the SQL models.

We modify the incremental SQL model by adding a new column to the query. Open the `models/incremental_model.sql` file and add `#!sql 'z' AS new_column` below `item_id` as follows:

```sql linenums="1" hl_lines="14"
MODEL (
  name sqlmesh_example.incremental_model,
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date
  ),
  start '2020-01-01',
  cron '@daily',
  grain (id, event_date)
);

SELECT
  id,
  item_id,
  'z' AS new_column, -- Added column
  event_date,
FROM
  sqlmesh_example.seed_model
WHERE
  event_date between @start_date and @end_date
```

## 4. Work with a development environment

### 4.1 Create a dev environment
Now that you've modified a model, it's time to create a development environment so that you can validate the model change without affecting production.

Run `sqlmesh plan dev` to create a development environment called `dev`:

```bash linenums="1"
$ sqlmesh plan dev
======================================================================
Successfully Ran 1 tests against duckdb
----------------------------------------------------------------------

New environment `dev` will be created from `prod`


Differences from the `prod` environment:

Models:
├── Directly Modified:
│   └── sqlmesh_example__dev.incremental_model
└── Indirectly Modified:
    └── sqlmesh_example__dev.full_model

---

+++

@@ -14,6 +14,7 @@

 SELECT
   id,
   item_id,
+  'z' AS new_column,
   event_date
 FROM sqlmesh_example.seed_model
 WHERE

Directly Modified: sqlmesh_example__dev.incremental_model
(Non-breaking)
└── Indirectly Modified Children:
    └── sqlmesh_example__dev.full_model (Indirect Non-breaking)
Models needing backfill:
└── sqlmesh_example__dev.incremental_model: [2020-01-01 - 2025-04-17]
Apply - Backfill Tables [y/n]:
```

Line 6 of the output states that a new environment `dev` will be created from the existing `prod` environment.

Lines 10-15 summarize the differences between the modified model and the `prod` environment, detecting that we directly modified `incremental_model` and that `full_model` was indirectly modified because it selects from the incremental model. Note that the model schemas are `sqlmesh_example__dev`, indicating that they are being created in the `dev` environment.

On line 31, we see that SQLMesh automatically classified the change as `Non-breaking` because it understood that the change was additive (added a column not used by `full_model`) and did not invalidate any data already in `prod`.

Enter `y` at the prompt and press `Enter` to apply the plan and execute the backfill:

```bash linenums="1"
Apply - Backfill Tables [y/n]: y

Updating physical layer ━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 2/2 • 0:00:00

✔ Physical layer updated

[1/1] sqlmesh_example__dev.incremental_model  [insert 2020-01-01 - 2025-04-17] 0.03s
Executing model batches ━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00

✔ Model batches executed

Updating virtual layer  ━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 2/2 • 0:00:00

✔ Virtual layer updated
```

Lines 3-5 show the progress and completion of updating the physical layer.

Line 7 shows that SQLMesh applied the change and evaluated `sqlmesh_example__dev.incremental_model` for the date range from 2020-01-01 to 2025-04-17.

Lines 9-11 show the progress and completion of executing model batches.

Lines 13-15 show the progress and completion of updating the virtual layer.

SQLMesh did not need to backfill anything for the `full_model` since the change was `Non-breaking`.

### 4.2 Validate updates in dev
You can now view this change by querying data from `incremental_model` with `sqlmesh fetchdf "select * from sqlmesh_example__dev.incremental_model"`.

Note that the environment name `__dev` is appended to the schema namespace `sqlmesh_example` in the query:

```bash
$ sqlmesh fetchdf "select * from sqlmesh_example__dev.incremental_model"

   id  item_id new_column  event_date
0   1        2          z  2020-01-01
1   2        1          z  2020-01-01
2   3        3          z  2020-01-03
3   4        1          z  2020-01-04
4   5        1          z  2020-01-05
5   6        1          z  2020-01-06
6   7        1          z  2020-01-07
```

You can see that `new_column` was added to the dataset. The production table was not modified; you can validate this by querying the production table using `sqlmesh fetchdf "select * from sqlmesh_example.incremental_model"`.

Note that nothing has been appended to the schema namespace `sqlmesh_example` in this query because `prod` is the default environment.

```bash
$ sqlmesh fetchdf "select * from sqlmesh_example.incremental_model"

   id  item_id   event_date
0   1        2   2020-01-01
1   2        1   2020-01-01
2   3        3   2020-01-03
3   4        1   2020-01-04
4   5        1   2020-01-05
5   6        1   2020-01-06
6   7        1   2020-01-07
```

The production table does not have `new_column` because the changes to `dev` have not yet been applied to `prod`.

## 5. Update the prod environment

### 5.1 Apply updates to prod
Now that we've tested the changes in dev, it's time to move them to production. Run `sqlmesh plan` to plan and apply your changes to the `prod` environment.

Enter `y` and press `Enter` at the `Apply - Virtual Update [y/n]:` prompt to apply the plan and execute the backfill:

```bash
$ sqlmesh plan
======================================================================
Successfully Ran 1 tests against duckdb
----------------------------------------------------------------------

Differences from the `prod` environment:

Models:
├── Directly Modified:
│   └── sqlmesh_example.incremental_model
└── Indirectly Modified:
    └── sqlmesh_example.full_model

---

+++

@@ -14,6 +14,7 @@

 SELECT
   id,
   item_id,
+  'z' AS new_column,
   event_date
 FROM sqlmesh_example.seed_model
 WHERE

Directly Modified: sqlmesh_example.incremental_model (Non-breaking)
└── Indirectly Modified Children:
    └── sqlmesh_example.full_model (Indirect Non-breaking)
Apply - Virtual Update [y/n]: y

SKIP: No physical layer updates to perform

SKIP: No model batches to execute

Updating virtual layer  ━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 2/2 • 0:00:00

✔ Virtual layer updated
```

Note that a backfill was not necessary and only a Virtual Update occurred, as indicated by the "SKIP: No physical layer updates to perform" and "SKIP: No model batches to execute" messages. This is because the changes were already calculated and executed in the `dev` environment, and SQLMesh is smart enough to recognize that it only needs to update the virtual references to the existing tables rather than recomputing everything.

### 5.2 Validate updates in prod
Double-check that the data updated in `prod` by running `sqlmesh fetchdf "select * from sqlmesh_example.incremental_model"`:

```bash
$ sqlmesh fetchdf "select * from sqlmesh_example.incremental_model"

   id  item_id new_column  event_date
0   1        2          z  2020-01-01
1   2        1          z  2020-01-01
2   3        3          z  2020-01-03
3   4        1          z  2020-01-04
4   5        1          z  2020-01-05
5   6        1          z  2020-01-06
6   7        1          z  2020-01-07
```

## 6. Next steps

Congratulations, you've now conquered the basics of using SQLMesh!

From here, you can:

* [Learn more about SQLMesh CLI commands](../reference/cli.md)
* [Set up a connection to a database or SQL engine](../guides/connections.md)
* [Learn more about SQLMesh concepts](../concepts/overview.md)
* [Join our Slack community](https://tobikodata.com/slack)