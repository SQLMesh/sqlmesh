# Installation

This page provides instructions for installing SQLMesh on your computer.

## Python virtual environment

It is recommended, but not required, that you use a python virtual environment with SQLMesh.

First, create the virtual environment:
```bash
python -m venv .venv
```

Then activate it:
```bash
source .venv/bin/activate
```

## Install SQLMesh core

Install the core SQLMesh library with `pip`:
```bash
pip install sqlmesh
```

## Install extras
Some SQLMesh functionality requires additional Python libraries, which are bundled with SQLMesh via "extras".

In your `pip` command, specify the extra's name in brackets to automatically install the additional libraries. For example, you install the SQLMesh Github CI/CD bot extras with `pip install "sqlmesh[github]"`.

There are two types of extras.

Some extras add features, like the SQLMesh VSCode extension or Github CI/CD bot:

??? info "Feature extras commands"
    | Feature             | `pip` command                   |
    | ------------------- | ------------------------------- |
    | VSCode extension    | `pip install "sqlmesh[lsp]"`    |
    | Github CI/CD bot    | `pip install "sqlmesh[github]"` |
    | dbt projects        | `pip install "sqlmesh[dbt]"`    |
    | dlt projects        | `pip install "sqlmesh[dlt]"`    |
    | Slack notifications | `pip install "sqlmesh[slack]"`  |
    | Development setup   | `pip install "sqlmesh[dev]"`    |
    | Browser UI          | `pip install "sqlmesh[web]"`    |
    | LLM SQL prompt      | `pip install "sqlmesh[llm]"`    |

Other extras are required to use specific SQL engines, like Bigquery or Postgres:

??? info "SQL engine extras commands"
    | SQL engine    | `pip` command                        |
    | ------------- | ------------------------------------ |
    | Athena        | `pip install "sqlmesh[athena]"`      |
    | Azure SQL     | `pip install "sqlmesh[azuresql]"`    |
    | Bigquery      | `pip install "sqlmesh[bigquery]"`    |
    | ClickHouse    | `pip install "sqlmesh[clickhouse]"`  |
    | Databricks    | `pip install "sqlmesh[databricks]"`  |
    | Fabric        | `pip install "sqlmesh[fabric]"`      |
    | GCP Postgres  | `pip install "sqlmesh[gcppostgres]"` |
    | MS SQL Server | `pip install "sqlmesh[mssql]"`       |
    | MotherDuck    | `pip install "sqlmesh[motherduck]"`  |
    | MySQL         | `pip install "sqlmesh[mysql]"`       |
    | Postgres      | `pip install "sqlmesh[postgres]"`    |
    | Redshift      | `pip install "sqlmesh[redshift]"`    |
    | RisingWave    | `pip install "sqlmesh[risingwave]"`  |
    | Snowflake     | `pip install "sqlmesh[snowflake]"`   |
    | StarRocks     | `pip install "sqlmesh[starrocks]"`   |
    | Trino         | `pip install "sqlmesh[trino]"`       |

Multiple extras can be installed at once, as in `pip install "sqlmesh[github,slack]"`.

## Next steps

Now that you've installed SQLMesh, it's time to get started with the SQLMesh example project.

SQLMesh has three user interfaces - choose one for the example project and jump right in:

- [Command line interface (CLI)](./quickstart/cli.md)
- [Notebook interface](./quickstart/notebook.md)
- [Browser UI graphical interface](./quickstart/ui.md)

Have an existing dbt project you want to run? Install the dbt extra and [check out SQLMesh's dbt adapter](./integrations/dbt.md).
