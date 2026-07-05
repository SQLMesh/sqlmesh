# External models

SQLMesh model queries may reference "external" tables that are created and managed outside the SQLMesh project. For example, a model might ingest data from a third party's read-only data system.

SQLMesh does not manage external tables, but it can use information about the tables' columns and data types to make features more useful. For example, column information allows column-level lineage to include external tables' columns.

SQLMesh stores external tables' column information as `EXTERNAL` models.

## External models are not run

`EXTERNAL` models consist solely of an external table's column information, so there is no query for SQLMesh to run.

SQLMesh has no information about the data contained in the table represented by an `EXTERNAL` model. The table could be altered or have all its data deleted, and SQLMesh will not detect it. All SQLMesh knows about the table is that it contains the columns specified in the `EXTERNAL` model's file (more information below).

SQLMesh will not take any actions based on an `EXTERNAL` model - its actions are solely determined by the model whose query selects from the `EXTERNAL` model.

The querying model's [`kind`](./model_kinds.md), [`cron`](./overview.md#cron), and previously loaded time intervals determine when SQLMesh will query the `EXTERNAL` model.

## Generating an external models schema file

External models can be defined in the `external_models.yaml` file in the SQLMesh project's root folder. The alternative name for this file is `schema.yaml`.

You can create this file by either writing the YAML by hand or allowing SQLMesh to fetch information about external tables with the `create_external_models` CLI command.

Consider this example model that queries an external table `external_db.external_table`:

```sql
MODEL (
  name my_db.my_table,
  kind FULL
);

SELECT
  *
FROM
  external_db.external_table;
```

The following sections demonstrate how to create an external model containing `external_db.external_table`'s column information.

All of a SQLMesh project's external models are defined in a single `external_models.yaml` file, so the files created below might also include column information for other external models.

Alternatively, additional external models can also be defined in the [external_models/](#using-the-external_models-directory) folder.

### Using CLI

Instead of creating the `external_models.yaml` file manually, SQLMesh can generate it for you with the [create_external_models](../../reference/cli.md#create_external_models) CLI command.

The command identifies all external tables referenced in your SQLMesh project, fetches their column information from the SQL engine's metadata, and then stores the information in the `external_models.yaml` file.

If SQLMesh does not have access to an external table's metadata, the table will be omitted from the file and SQLMesh will issue a warning.

`create_external_models` solely queries SQL engine metadata and does not query external tables themselves.

By default, the command overwrites same-gateway entries with freshly fetched columns and types. Use `--mode sync` to preserve any hand-edited metadata (such as descriptions, audits, or start dates) while only updating columns from the database. Use `--mode sync_prune` to additionally remove entries that are no longer referenced by any model.

### Gateway-specific external models

In some use-cases such as [isolated systems with multiple gateways](../../guides/isolated_systems.md#multiple-gateways), there are external models that only exist on a certain gateway.

**Gateway names are case-insensitive in external model configurations.** You can specify the gateway name using any case (e.g., `gateway: dev`, `gateway: DEV`, `gateway: Dev`) and SQLMesh will handle the matching correctly.

Consider the following model that queries an external table with a dynamic database based on the current gateway:

```
MODEL (
  name my_db.my_table,
  kind FULL
);

SELECT
  *
FROM
  @{gateway}_db.external_table;
```

This table will be named differently depending on which `--gateway` SQLMesh is run with (learn more about the curly brace `@{gateway}` syntax [here](../../concepts/macros/sqlmesh_macros.md#embedding-variables-in-strings)).

For example:

- `sqlmesh --gateway dev plan` - SQLMesh will try to query `dev_db.external_table`
- `sqlmesh --gateway prod plan` - SQLMesh will try to query `prod_db.external_table`

To ensure SQLMesh can look up the correct schema when the relevant gateway is set, run `create_external_models` with the `--gateway` argument. For example:

- `sqlmesh --gateway dev create_external_models`

This will set `gateway: dev` on the external model and ensure that it is only loaded when the current gateway is set to `dev`.

### Writing YAML by hand

This example demonstrates the structure of a `external_models.yaml` file:

```yaml
- name: external_db.external_table
  description: An external table
  columns:
    column_a: int
    column_b: text
- name: external_db.some_other_external_table
  description: Another external table
  columns:
    column_c: bool
    column_d: float
- name: external_db.gateway_specific_external_table
  description: Another external table that only exists when the gateway is set to "test"
  gateway: test  # Case-insensitive - could also be "TEST", "Test", etc.
  columns:
    column_e: int
    column_f: varchar
```

It contains each `EXTERNAL` model's name, an optional description, an optional gateway and each of the external table's columns' name and data type.

The file can be constructed by hand using a standard text editor or IDE.

### Using the `external_models` directory

Sometimes, SQLMesh cannot infer the structure of a model and you need to add it manually.

By default, running `sqlmesh create_external_models` replaces the entries in `external_models.yaml` — any manual changes to that file may be lost. To preserve hand-edited metadata such as descriptions and audits, pass `--mode sync`, which only updates columns from the database while keeping all other fields.

Alternatively, you can place manual model definitions in the `external_models/` directory, like so:

```
external_models.yaml
external_models/more_external_models.yaml
external_models/even_more_external_models.yaml
```

Files in the `external_models` directory must be `.yaml` files that follow the same structure as the `external_models.yaml` file.

When SQLMesh loads the definitions, it will first load the models defined in `external_models.yaml` (or `schema.yaml`) and  any models found in `external_models/*.yaml`.

Therefore, you can use `sqlmesh create_external_models` to manage the `external_models.yaml` file and then put any models that need to be defined manually inside the `external_models/` directory.

### External Audits
It is possible to define [audits](../audits.md) on external models. This can be useful to check the data quality of upstream dependencies before your internal models evaluate.

This example shows an external model with two audits.

```yaml
- name: raw.demographics
  description: Table containing demographics information
  audits:
    - name: not_null
      columns: "[customer_id]"
    - name: accepted_range
      column: zip
      min_v: "'00000'"
      max_v: "'99999'"
  columns:
    customer_id: int
    zip: text
```
