# Models guide

## Prerequisites

---

Before adding a model, ensure that you have [already created your project](./projects.md) and that you are working in a [dev environment](../concepts/environments.md).

---

## Adding a model

To add a model:

1. Within your `models` folder, create a new file. For example, we might add `new_model.sql` to the [quickstart](../quick_start.md) project.
2. Within the file, define a model. For example:

        MODEL (
          name sqlmesh_example.new_model,
          kind INCREMENTAL_BY_TIME_RANGE (
            time_column (model_time_column, '%Y-%m-%d'),
          ),
        );

        SELECT *
        FROM sqlmesh_example.incremental_model
        WHERE model_time_column BETWEEN @start_ds and @end_ds

    **Note:** The last line in this file is required if your model is incremental. Refer to [model kinds](../concepts/models/model_kinds.md) for more information about the kinds of models you can create.

## Editing an existing model

To edit an existing model:

1. Open the model file you wish to edit in your preferred editor and make a change.
2. To preview an example of what your change looks like without actually creating a table, use the `sqlmesh evaluate` command. Refer to [evaluating a model](#evaluating-a-model) below.
3. To materialize this change, use the `sqlmesh plan` command. Refer to [previewing changes using the `plan` command](#previewing-changes-using-the-plan-command) below.

### Evaluating a model

The `evaluate` command will run a query against your database or engine and return a dataframe. It is used to test or iterate on models without database side effects and at minimal cost because SQLMesh isn't materializing any data.

To evaluate a model:

1. Run the `evaluate` command using either the [CLI](../reference/cli.md) or [Notebook](../reference/notebook.md). For example, running the `evaluate` command on `incremental_model` from the [quickstart](../quick_start.md) project:

        $ sqlmesh evaluate sqlmesh_example.incremental_model --start=2020-01-07 --end=2020-01-07

        id  item_id          model_time_column
        0   7        1  2020-01-07

2. When you run the `evaluate` command, SQLMesh detects the changes made to the model, executes the model as a query using the options passed to `evaluate`, and shows the output returned by the model query.

### Previewing changes using the `plan` command

When SQLMesh runs the `plan` command on your environment, it will show you whether any downstream models are impacted by your changes. If so, SQLMesh will prompt you to classify the changes as [Breaking](../concepts/plans.md#breaking-change) or [Non-Breaking](../concepts/plans.md#non-breaking-change) before applying the changes.

To preview changes using `plan`:

1. Enter the `sqlmesh plan <environment name>` command.
2. Enter `1` to classify the changes as `Breaking`, or enter `2` to classify the changes as `Non-Breaking`. In this example, the changes are classified as `Non-Breaking`:

```bash linenums="1" hl_lines="27-28"
$ sqlmesh plan dev
======================================================================
Successfully Ran 1 tests against duckdb
----------------------------------------------------------------------
New environment `dev` will be created from `prod`

Differences from the `prod` environment:

Models
├── Directly Modified:
│   └── sqlmesh_example.incremental_model
└── Indirectly Modified:
    └── sqlmesh_example.full_model
---
+++
@@ -1,6 +1,7 @@
SELECT
id,
item_id,
+  1 AS new_column,
model_time_column
FROM (VALUES
(1, 1, '2020-01-01'),
Directly Modified: sqlmesh_example.incremental_model
└── Indirectly Modified Children:
    └── sqlmesh_example.full_model
[1] [Breaking] Backfill sqlmesh_example.incremental_model and indirectly modified children
[2] [Non-breaking] Backfill sqlmesh_example.incremental_model but not indirectly modified children: 2
Models needing backfill (missing dates):
└── sqlmesh_example.incremental_model: (2020-01-01, 2023-02-17)
Enter the backfill start date (eg. '1 year', '2020-01-01') or blank for the beginning of history:
Enter the backfill end date (eg. '1 month ago', '2020-01-01') or blank to backfill up until now:
Apply - Backfill Tables [y/n]: y

sqlmesh_example__dev.incremental_model ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00

All model batches have been executed successfully

Virtually Updating 'dev' ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 0:00:00

The target environment has been updated successfully
```

For more information, refer to [plans](../concepts/plans.md).

## Reverting a change to a model

---

Before trying to revert a change, ensure that you have already made a change and that you have run the `sqlmesh plan` command.

---

To revert your change:

1. Open the model file you wish to edit in your preferred editor, and undo a change you made earlier. For this example, we'll remove the column we added in the [quickstart](../quick_start.md) example.
2. Run `sqlmesh plan` and apply your changes. Enter `y` to run a Virtual Update.

```bash linenums="1" hl_lines="26"
$ sqlmesh plan dev
======================================================================
Successfully Ran 1 tests against duckdb
----------------------------------------------------------------------
Differences from the `dev` environment:

Models
├── Directly Modified:
│   └── sqlmesh_example.incremental_model
└── Indirectly Modified:
    └── sqlmesh_example.full_model
---
+++
@@ -1,7 +1,6 @@

SELECT
id,
item_id,
-  1 AS new_column,
model_time_column
FROM (VALUES
    (1, 1, '2020-01-01'),
Directly Modified: sqlmesh_example.incremental_model (Non-breaking)
└── Indirectly Modified Children:
    └── sqlmesh_example.full_model
Apply - Virtual Update [y/n]: y

Virtual Update executed successfully
```
### Virtual Update

Reverting to a previous model version is a quick operation since no additional work is being done. For more information, refer to [plan application](../concepts/plans.md#plan-application) and [Virtual Update](../concepts/plans.md#virtual-update).

**Note:** The SQLMesh janitor runs periodically and automatically to clean up SQLMesh artifacts no longer being used, and determines the time-to-live (TTL) for tables (how much time can pass before reverting is no longer possible).

## Validating changes to a model

### Automatic model validation

SQLMesh automatically validates your models in order to ensure the quality and accuracy of your data. This is done via the following:

* Running unit tests for models in the plan when you execute the `plan` command (use `--all-tests` for the full suite, or `--skip-tests` to skip). This ensures changes applied to any environment are logically validated. Refer to [testing](../concepts/tests.md) for more information.

    !!! important

        `--skip-backfill` / `--dry-run` plans skip unit tests by default. Pass `--all-tests` if you still want tests to run.
* Running audits whenever data is loaded to a table (either for backfill or loading on a cadence). This way you know all data present in any table has passed all defined audits. Refer to [auditing](../concepts/audits.md) for more information.

SQLMesh also provides automatic validation via CI/CD by automatically creating a preview environment.

### Manual model validation

To manually validate your models, you can perform one or more of the following tasks:

* [Evaluating a model](#evaluating-a-model)
* [Testing a model using unit tests](../guides/testing.md#auditing-changes-to-models)
* [Auditing a model](../guides/testing.md#auditing-changes-to-models)
* [Previewing changes using the `plan` command](#previewing-changes-using-the-plan-command)

## Deleting a model

---

Before deleting a model, ensure that you have already run `sqlmesh plan`.

---

To delete a model:

1. Within your `models` directory, delete the file containing the model and any associated tests in the `tests` directory. For this example, we'll delete the `models/full_model.sql` and `tests/test_full_model.yaml` files from our [quickstart](../quick_start.md) project.
2. Run the `sqlmesh plan <environment>` command, specifying the environment to which you want to apply the change. In this example, we apply the change to our development environment `dev`:

        ```bash linenums="1"
        $ sqlmesh plan dev
        ======================================================================
        Successfully Ran 0 tests against duckdb
        ----------------------------------------------------------------------
        Differences from the `dev` environment:

        Models
        └── Removed Models:
            └── sqlmesh_example.full_model
        Apply - Virtual Update [y/n]: y
        Virtually Updating 'dev' ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 0:00:00

        The target environment has been updated successfully


        Virtual Update executed successfully
        ```

    **Note:** If you have other files that reference the model you wish to delete (such as tests), an error message will note the file(s) containing the reference. You must also delete these files to apply the change.

3. Plan and apply your changes to production, and enter `y` for the Virtual Update. By default, the `sqlmesh plan` command targets your production environment:

        ```bash linenums="1"
        $ sqlmesh plan
        ======================================================================
        Successfully Ran 0 tests against duckdb
        ----------------------------------------------------------------------
        Differences from the `prod` environment:

        Models
        └── Removed Models:
            └── sqlmesh_example.full_model
        Apply - Virtual Update [y/n]: y
        Virtually Updating 'prod' ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 0:00:00

        The target environment has been updated successfully


        Virtual Update executed successfully
        ```

4. Verify that the `full_model.sql` model was removed from the output.

## Viewing the DAG of a project's models

---

Before generating a DAG, ensure that you have already installed the graphviz package.

To install the package with `pip`, enter the following command:

```bash
pip install graphviz
```

Alternatively, enter the following command to install graphviz with `apt-get`:

```bash
sudo apt-get install graphviz
```

---

To view the DAG, enter the following command:

`sqlmesh dag FILE`

An html file containing your project's DAG will be placed at the root of your project folder. The DAG can then be viewed by opening this file in your browser.