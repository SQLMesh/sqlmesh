# Multi-Repo guide

Although mono repos are convenient and easy to use, sometimes your organization may choose to use multiple repos.
SQLMesh provides native support for multiple repos and makes it easy to maintain data consistency and correctness even with multiple repos.
If you are wanting to separate your systems/data and provide isolation, checkout the [isolated systems guide](https://sqlmesh.readthedocs.io/en/stable/guides/isolated_systems/?h=isolated).

## Bootstrapping multiple projects
Setting up SQLMesh with multiple repos is quite simple. Copy the contents of this example [multi-repo project](https://github.com/SQLMesh/sqlmesh/tree/main/examples/multi).

To bootstrap the project, you can point SQLMesh at both projects.

```
$ sqlmesh -p examples/multi/repo_1 -p examples/multi/repo_2/ plan
======================================================================
Successfully Ran 0 tests against duckdb
----------------------------------------------------------------------
`prod` environment will be initialized

Models
└── Added:
    ├── silver.d
    ├── bronze.a
    ├── bronze.b
    └── silver.c
Models needing backfill (missing dates):
├── bronze.a: (2023-04-17, 2023-04-17)
├── bronze.b: (2023-04-17, 2023-04-17)
├── silver.d: (2023-04-17, 2023-04-17)
└── silver.c: (2023-04-17, 2023-04-17)
Apply - Backfill Tables [y/n]: y
bronze.a ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00
silver.c ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00
bronze.b ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00
silver.d ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00

All model batches have been executed successfully

Virtually Updating 'prod' ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 0:00:00

The target environment has been updated successfully
```

You can see that all 4 models were planned and applied even though bronze is in repo_1 and silver is in repo_2.

## Editing and planning one project

Make a non-breaking change to bronze.a by adding column c.

```
--- a/examples/multi/repo_1/models/a.sql
+++ b/examples/multi/repo_1/models/a.sql
@@ -4,4 +4,5 @@ MODEL (

 SELECT
   1 AS col_a,
-  'b' AS col_b
+  'b' AS col_b,
+  'c' AS col_c
```

Run a plan with just repo_1.

```
$ sqlmesh -p examples/multi/repo_1 plan
======================================================================
Successfully Ran 0 tests against duckdb
----------------------------------------------------------------------
Differences from the `prod` environment:

Models
├── Directly Modified:
│   └── bronze.a
└── Indirectly Modified:
    ├── bronze.b
    ├── silver.d
    └── silver.c
---

+++

@@ -1,3 +1,4 @@

 SELECT
   1 AS col_a,
-  'b' AS col_b
+  'b' AS col_b,
+  'c' AS col_c
Directly Modified: bronze.a (Non-breaking)
└── Indirectly Modified Children:
    ├── silver.c
    ├── bronze.b
    └── silver.d
Models needing backfill (missing dates):
└── bronze.a: (2023-04-17, 2023-04-17)
Apply - Backfill Tables [y/n]: y
bronze.a ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00

All model batches have been executed successfully

Virtually Updating 'prod' ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 0:00:00

The target environment has been updated successfully
```

SQLMesh detects the entire lineage of the non-breaking change even though you only have one project "checked out".

## Make a breaking change and backfill
Change col_a to 1 + 1.

```
--- a/examples/multi/repo_1/models/a.sql
+++ b/examples/multi/repo_1/models/a.sql
@@ -3,5 +3,6 @@ MODEL (
 );

 SELECT
-  1 AS col_a,
-  'b' AS col_b
+  1 + 1 AS col_a,
+  'b' AS col_b,
+  'c' AS col_c
```

```
$ sqlmesh -p examples/multi/repo_1 plan
======================================================================
Successfully Ran 0 tests against duckdb
----------------------------------------------------------------------
Differences from the `prod` environment:

Models
├── Directly Modified:
│   └── bronze.a
└── Indirectly Modified:
    ├── bronze.b
    ├── silver.d
    └── silver.c
---

+++

@@ -1,4 +1,4 @@

 SELECT
-  1 AS col_a,
+  1 + 1 AS col_a,
   'b' AS col_b,
   'c' AS col_c
Directly Modified: bronze.a (Breaking)
└── Indirectly Modified Children:
    ├── silver.d
    ├── bronze.b
    └── silver.c
Models needing backfill (missing dates):
├── bronze.a: (2023-04-17, 2023-04-17)
├── bronze.b: (2023-04-17, 2023-04-17)
├── silver.d: (2023-04-17, 2023-04-17)
└── silver.c: (2023-04-17, 2023-04-17)
Apply - Backfill Tables [y/n]: y
bronze.a ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00
silver.c ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00
bronze.b ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00
silver.d ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00

All model batches have been executed successfully

Virtually Updating 'prod' ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 0:00:00

The target environment has been updated successfully
```

SQLMesh correctly detects a breaking change and allows you to perform a multi-repo backfill.

## Configuring projects with multiple repositories

To add support for multiple repositories, add a `project` key to the config file in each of the respective repos.

```yaml
project: repo_1

gateways:
...
```

Even if you do not have a need for multiple repos now, consider adding a `project` key so that you can easily support multiple repos in the future.

## Running migrations with multiple repositories

When doing a [migration](./migrations.md), pass in a single repo path using the `-p` flag. It doesn't matter which repo you choose.

```
$ sqlmesh -p examples/multi/repo_1 migrate
```

## Multi-Repo dbt projects

SQLMesh also supports multiple repos for dbt projects, allowing it to correctly detect changes and orchestrate backfills even when changes span multiple dbt projects.

You can watch a [quick demo](https://www.loom.com/share/69c083428bb348da8911beb2cd4d30b2) of this setup or experiment with the [multi-repo dbt example](https://github.com/SQLMesh/sqlmesh/tree/main/examples/multi_dbt) yourself.

## Multi-repo mixed projects

Native SQLMesh projects can be used alongside dbt projects in a multi-repo setup.

This allows managing and sourcing tables from either project type within the same multi-repo project and facilitates a gradual migration from dbt to SQLMesh.

Use the same syntax as SQLMesh-only multi-repo projects to execute a multi-repo project with either dbt or a combination of dbt and SQLMesh projects:

```
$ sqlmesh -p examples/multi_hybrid/dbt_repo -p examples/multi_hybrid/sqlmesh_repo plan
```

SQLMesh will automatically detect dependencies and lineage across both SQLMesh and dbt projects, even when models are sourcing from different project types.

For an example of this setup, refer to the [mixed SQLMesh and dbt example](https://github.com/SQLMesh/sqlmesh/tree/main/examples/multi_hybrid).
