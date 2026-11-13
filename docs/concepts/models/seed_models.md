# Seed models

A `SEED` is a special kind of model in which data is sourced from a static dataset defined as a CSV file (rather than from a data source accessed via SQL or Python). The CSV files themselves are a part of your SQLMesh project.

Since seeds are also models in SQLMesh, they capitalize on all the same benefits that SQL or Python models provide:

* A physical table is created in the data warehouse, which reflects the contents of the seed's CSV file.
* Seed models can be referenced in downstream models in the same way as other models.
* Changes to CSV files are captured during [planning](../plans.md#plan-application) and versioned using the same [fingerprinting](../architecture/snapshots.md#fingerprinting) mechanism.
* [Environment](../environments.md) isolation also applies to seed models.

Seed models are a good fit for static datasets that change infrequently or not at all. Examples of such datasets include:

* Names of national holidays and their dates
* A static list of identifiers that should be excluded

!!! warning "Not supported in Python models"

    Python models do not support the `SEED` [model kind](./model_kinds.md) - use a SQL model instead.

## Creating a seed model

Similar to [SQL models](./sql_models.md), `SEED` models are defined in files with the `.sql` extension in the `models/` directory of the SQLMesh project.

Use the special kind `SEED` in the `MODEL` definition to indicate that the model is a seed model:

```sql linenums="1" hl_lines="3-5"
MODEL (
  name test_db.national_holidays,
  kind SEED (
    path 'national_holidays.csv'
  )
);
```
The `path` attribute contains the path to the seed's CSV file **relative** to the path of the model's `.sql` file. If you want to specify a path relative to the root of the SQLMesh project, use the `$root` marker (see [Markers](#markers)).

If your seed file has special quoting rules or delimiters, you can pass settings to [Pandas' `read_csv` function](https://pandas.pydata.org/docs/reference/api/pandas.read_csv.html) with the `csv_settings` dictionary (all supported settings [here](../../reference/model_configuration.md#csv_settings)):

```sql linenums="1" hl_lines="5-7"
MODEL (
  name test_db.national_holidays,
  kind SEED (
    path 'national_holidays.csv',
    csv_settings (
      delimiter = "|"
    )
  )
);
```

The physical table with the seed CSV's content is created using column types inferred by Pandas. Alternatively, you can manually specify the dataset schema as part of the `MODEL` definition:
```sql linenums="1" hl_lines="9-12"
MODEL (
  name test_db.national_holidays,
  kind SEED (
    path 'national_holidays.csv',
    csv_settings (
      delimiter = "|"
    )
  ),
  columns (
    name VARCHAR,
    date DATE
  )
);
```
**Note:** The dataset schema provided in the definition takes precedence over column names defined in the header of the CSV file. This means that the order in which columns are provided in the `MODEL` definition must match the order of columns in the CSV file.

Declaring `columns` also controls how the CSV values are read. Columns with a text type such as `VARCHAR` or `TEXT` keep their values exactly as written in the CSV, so identifiers like ZIP codes or account numbers keep their leading zeros (`00123` is loaded as `00123`, not `123`). Empty values, and the NA markers controlled by `csv_settings` such as `na_values` and `keep_default_na`, are still loaded as `NULL`. Integer columns that contain empty values are loaded as integers with `NULL`s rather than floats. If `columns` is omitted, types are inferred by Pandas as described above.

### Markers

The `$root` marker can be used in the `path` attribute to indicate that the CSV file path is relative to the root of the SQLMesh project:

```sql linenums="1" hl_lines="3-5"
MODEL (
  name test_db.national_holidays,
  kind SEED (
    path '$root/seeds/national_holidays.csv'
  )
);
```

This is useful when you want to keep all seed CSV files in a top-level directory such as `seeds/` but don't want to keep track of or manage a bunch of relative paths.

### Encoding

SQLMesh expects seed files to be encoded according to the [UTF-8](https://en.wikipedia.org/wiki/UTF-8) standard. Using a different encoding may lead to unexpected behavior.

## Example

In this example, we use the model definition from the previous section saved in the `models/national_holidays.sql` file of the SQLMesh project.

We also add the seed CSV file itself in the `models/` directory as a CSV file named `national_holidays.csv` with the following contents:

```csv linenums="1"
name,date
New Year,2023-01-01
Christmas,2023-12-25
```

When we run the `sqlmesh plan` command, the new seed model is automatically detected:
```bash hl_lines="8-9"
$ sqlmesh plan
======================================================================
Successfully Ran 0 tests against duckdb
----------------------------------------------------------------------
`prod` environment will be initialized

Models
└── Added:
    └── test_db.national_holidays
Models needing backfill (missing dates):
└── test_db.national_holidays: (2023-02-16, 2023-02-16)
Enter the backfill start date (eg. '1 year', '2020-01-01') or blank for the beginning of history:
Apply - Backfill Tables [y/n]: y

All model batches have been executed successfully

test_db.national_holidays ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00
```

Applying the plan created a new table `test_db.national_holidays`.

You can now run a custom query against the table with `sqlmesh fetchdf`:
```bash
$ sqlmesh fetchdf "SELECT * FROM test_db.national_holidays"

        name        date
0   New Year  2023-01-01
1  Christmas  2023-12-25
```

Changes to the seed CSV file get picked up when the `sqlmesh plan` command is run:
```bash
$ sqlmesh plan
======================================================================
Successfully Ran 0 tests against duckdb
----------------------------------------------------------------------
Differences from the `prod` environment:

Models:
└── Directly Modified:
    └── test_db.national_holidays
---

+++

@@ -1,3 +1,4 @@

 name,date
 New Year,2023-01-01
 Christmas,2023-12-25
+Independence Day,2023-07-04
Directly Modified: test_db.national_holidays
[1] [Breaking] Backfill test_db.national_holidays and indirectly modified children
[2] [Non-breaking] Backfill test_db.national_holidays but not indirectly modified children: 1
Models needing backfill (missing dates):
└── test_db.national_holidays: (2023-02-16, 2023-02-16)
Enter the backfill start date (eg. '1 year', '2020-01-01') or blank for the beginning of history:
Apply - Backfill Tables [y/n]: y

All model batches have been executed successfully

test_db.national_holidays ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 1/1 • 0:00:00
```

## Pre- and post-statements

Seed models also support pre- and post-statements, which are evaluated before inserting the seed's content and after, respectively.

Below is an example that only involves pre-statements:

```sql linenums="1" hl_lines="8"
MODEL (
  name test_db.national_holidays,
  kind SEED (
    path 'national_holidays.csv'
  )
);

ALTER SESSION SET TIMEZONE = 'UTC';
```

To add post-statements, you should use the special `@INSERT_SEED()` macro to separate pre- and post-statements:

```sql linenums="1" hl_lines="11"
MODEL (
  name test_db.national_holidays,
  kind SEED (
    path 'national_holidays.csv'
  )
);

-- These are pre-statements
ALTER SESSION SET TIMEZONE = 'UTC';

@INSERT_SEED();

-- These are post-statements
ALTER SESSION SET TIMEZONE = 'PST';
```

## On-virtual-update statements

Seed models also support on-virtual-update statements, which are executed after the completion of the [Virtual Update](#virtual-update).

**Project-level defaults:** You can also define on-virtual-update statements at the project level using `model_defaults` in your configuration. These will be applied to all models in your project (including seed models) and merged with any model-specific statements. Default statements are executed first, followed by model-specific statements. Learn more about this in the [model configuration reference](../../reference/model_configuration.md#model-defaults).

These must be enclosed within an `ON_VIRTUAL_UPDATE_BEGIN;` ...; `ON_VIRTUAL_UPDATE_END;` block:

```sql linenums="1" hl_lines="8-13"
MODEL (
  name test_db.national_holidays,
  kind SEED (
    path 'national_holidays.csv'
  )
);

ON_VIRTUAL_UPDATE_BEGIN;
GRANT SELECT ON VIEW @this_model TO ROLE dev_role;
JINJA_STATEMENT_BEGIN;
GRANT SELECT ON VIEW {{ this_model }} TO ROLE admin_role;
JINJA_END;
ON_VIRTUAL_UPDATE_END;
```


[Jinja expressions](../macros/jinja_macros.md) can also be used within them, as demonstrated in the example above. These expressions must be properly nested within a `JINJA_STATEMENT_BEGIN;` and `JINJA_END;` block.

!!! note

    Table resolution for these statements occurs at the virtual layer. This means that table names, including `@this_model` macro, are resolved to their qualified view names. For instance, when running the plan in an environment named `dev`, `db.customers` and `@this_model` would resolve to `db__dev.customers` and not to the physical table name.