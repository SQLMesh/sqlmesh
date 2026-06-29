# Overview

This page provides a conceptual overview of what SQLMesh does and how its components fit together.

## What SQLMesh is
SQLMesh is a Python framework that automates everything needed to run a scalable data transformation platform. SQLMesh works with a variety of [engines and orchestrators](../integrations/overview.md).

It was created with a focus on both data and organizational scale and works regardless of your data warehouse or SQL engine's capabilities.

You can use SQLMesh with the [CLI](../reference/cli.md), [Notebook](../reference/notebook.md), or [Python](../reference/python.md) APIs.

## How SQLMesh works
### Create models
You begin by writing your business logic in SQL or Python. A model consists of code that populates a single table or view, along with metadata properties such as the model's name.

### Make a plan
Creating new models or changing existing models can have dramatic downstream effects in large data systems. Complex interdependencies between models make it challenging to determine the implications of changes to even a single model.

Beyond understanding the logical implications of a change, you also need to understand the computations required to implement the change *before* you expend the time and resources to actually perform the computations.

SQLMesh automatically identifies all affected models and the computations a change entails by creating a "SQLMesh plan." When you execute the [`plan` command](../reference/cli.md#plan), SQLMesh generates the plan *for the environment specified in the command* (e.g., dev, test, prod).

The plan conveys the full scope of a change's effects in the environment by automatically identifying both directly and indirectly-impacted models. This gives a holistic view of all impacts a change will have.

Learn more about [plans](./plans.md).

#### Apply the plan
After using [`plan`](../reference/cli.md#plan) to understand the impacts of a change in an environment, SQLMesh offers to execute the computations by [`apply`](./plans.md#plan-application)ing the plan. However, you must provide additional information that determines the scope of what computations are executed.

The computations needed to apply a SQLMesh plan are determined by both the code changes reflected in the plan and the backfill parameters you specify.

"Backfilling" is the process of updating existing data to align with your changed models. For example, if your model change alters a calculation, then all existing data based on the old calculation method will be inaccurate once the new model is deployed. Backfilling entails re-calculating the existing fields whose calculation method has now changed.

Most business data is temporal &mdash; each data fact was collected at a specific moment in time. The scale of backfill computations is directly tied to how much historical data must be re-calculated.

The SQLMesh plan automatically determines which models and dates require backfill due to your changes. Based on this information, you specify the dates for which backfills will occur before you apply the plan.

#### Build a Virtual Environment
Development activities for complex data systems should occur in a non-production environment so that errors can be detected before being deployed in production systems.

One challenge with using multiple data environments is that backfill and other computations must happen twice &mdash; once for the non-production, and again for the production environment. This process consumes time and computing resources, resulting in delays and extra costs.

SQLMesh solves this problem by maintaining a record of all model versions and their changes. It uses this record to determine when computations executed in a non-production environment generate outputs identical to what they would generate in the production environment.

SQLMesh uses its knowledge of equivalent outputs to create a **Virtual Environment**. It does this by replacing references to outdated tables in the production environment with references to newly computed tables in the non-production environment. It effectively promotes views and tables from non-production to production, but *without computation or data movement*.

Because SQLMesh uses virtual environments instead of re-computing everything in the production environment, promoting changes to production is quick and has no downtime.

## Test your code and data
Bad data is worse than no data. The best way to keep bad data out of your system is by testing your transformation code and results.

### [Tests](./tests.md)
SQLMesh "tests" are similar to unit tests in software development, where the unit is a single model. SQLMesh tests validate model *code* &mdash; you specify the input data and expected output, then SQLMesh runs the test and compares the expected and actual output.

SQLMesh automatically runs tests when you apply a `plan`, or you can run them on demand with the [`test` command](../reference/cli.md#test).

### [Audits](./audits.md)
In contrast to tests, SQLMesh "audits" validate the results of model code applied to your actual data.

You create audits by writing SQL queries that should return 0 rows. For example, an audit query to ensure `your_field` has no `NULL` values would include `WHERE your_field IS NULL`. If any NULLs are detected, the query will return at least one row and the audit will fail.

Audits are flexible &mdash; they can be tied to a specific model's contents, or you can use [macros](./macros/overview.md) to create audits that are usable by multiple models. SQLMesh also includes pre-made audits for common use cases, such as detecting NULL or duplicated values.

You specify which audits should run for a model by including them in the model's metadata properties. To apply them globally across your project, include them in the model defaults configuration.

SQLMesh automatically runs audits when you apply a `plan` to an environment, or you can run them on demand with the [`audit` command](../reference/cli.md#audit).

## Infrastructure and orchestration
Every company's data infrastructure is different. SQLMesh is flexible with regard to which engines and orchestration frameworks you use &mdash; its only requirement is access to the target SQL/analytics engine.

SQLMesh keeps track of model versions and processed data intervals using your existing infrastructure. It automatically creates a `sqlmesh` schema in your data warehouse for its internal metadata.
