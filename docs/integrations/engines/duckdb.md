# DuckDB

!!! warning "DuckDB state connection limitations"
    DuckDB is a [single user](https://duckdb.org/docs/connect/concurrency.html#writing-to-duckdb-from-multiple-processes) database. Using it for a state connection in your SQLMesh project limits you to a single workstation. This means your project cannot be shared amongst your team members or your CI/CD infrastructure. This is usually fine for proof of concept or test projects but it will not scale to production usage.

    For production projects, use [Tobiko Cloud](https://tobikodata.com/product.html) or a more robust state database such as [Postgres](./postgres.md).

## Local/Built-in Scheduler
**Engine Adapter Type**: `duckdb`

### Connection options

| Option             | Description                                                                                                                                                                                                                                     |   Type    | Required |
|--------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:---------:|:--------:|
| `type`             | Engine type name - must be `duckdb`                                                                                                                                                                                                             |  string   |    Y     |
| `database`         | The optional database name. If not specified, the in-memory database is used. Cannot be defined if using `catalogs`.                                                                                                                            |  string   |    N     |
| `catalogs`         | Mapping to define multiple catalogs. Can [attach DuckDB catalogs](#duckdb-catalogs-example) or [catalogs for other connections](#other-connection-catalogs-example). First entry is the default catalog. Cannot be defined if using `database`. |   dict    |    N     |
| `extensions`       | Extension to load into duckdb. Only autoloadable extensions are supported.                                                                                                                                                                      |   list    |    N     |
| `connector_config` | Configuration to pass into the duckdb connector.                                                                                                                                                                                                |   dict    |    N     |
| `secrets`          | Configuration for authenticating external sources (e.g., S3) using DuckDB secrets. Can be a list of secret configurations or a dictionary with custom secret names.                                                                             | list/dict |    N     |
| `filesystems`      | Configuration for registering `fsspec` filesystems to the DuckDB connection.                                                                                                                                                                    |   dict    |    N     |

#### DuckDB Catalogs Example

This example specifies two catalogs. The first catalog is named "persistent" and maps to the DuckDB file database `local.duckdb`. The second catalog is named "ephemeral" and maps to the DuckDB in-memory database.

`persistent` is the default catalog since it is the first entry in the dictionary. SQLMesh will place models without an explicit catalog, such as `my_schema.my_model`, into the `persistent` catalog `local.duckdb` DuckDB file database.

SQLMesh will place models with the explicit catalog "ephemeral", such as `ephemeral.other_schema.other_model`, into the `ephemeral` catalog DuckDB in-memory database.

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        connection:
          type: duckdb
          catalogs:
            persistent: 'local.duckdb'
            ephemeral: ':memory:'
    ```

=== "Python"

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
                connection=DuckDBConnectionConfig(
                    catalogs={
                        "persistent": "local.duckdb"
                        "ephemeral": ":memory:"
                    }
                )
            ),
        }
    )
    ```

#### DuckLake Catalog Example

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        connection:
          type: duckdb
          catalogs:
            ducklake:
              type: ducklake
              path: 'catalog.ducklake'
              data_path: data/ducklake
              override_data_path: true
              encrypted: True
              data_inlining_row_limit: 10
              metadata_schema: main
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        DuckDBConnectionConfig
    )
    from sqlmesh.core.config.connection import DuckDBAttachOptions

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    catalogs={
                        "ducklake": DuckDBAttachOptions(
                            type="ducklake",
                            path="catalog.ducklake",
                            data_path="data/ducklake",
                            override_data_path=False,
                            encrypted=True,
                            data_inlining_row_limit=10,
                            metadata_schema="main",
                        ),
                    }
                )
            ),
        }
    )
    ```

**DuckLake Configuration Options:**

- `path`: Path to the DuckLake catalog file
- `data_path`: Path where DuckLake data files are stored
- `override_data_path`: Whether data_override_path option is set
- `encrypted`: Whether to enable encryption for the catalog (default: `False`)
- `data_inlining_row_limit`: Maximum number of rows to inline in the catalog (default: `0`)
- `metadata_schema`: The schema in the catalog server in which to store the DuckLake metadata tables (default: `main`)

#### Other Connection Catalogs Example

Catalogs can also be defined to connect to anything that [DuckDB can be attached to](https://duckdb.org/docs/sql/statements/attach.html).

Below are examples of connecting to a SQLite database and a PostgreSQL database.
The SQLite database is read-write, while the PostgreSQL database is read-only.

=== "YAML"

    ```yaml linenums="1"
    gateways:
      my_gateway:
        connection:
          type: duckdb
          catalogs:
            memory: ':memory:'
            sqlite:
              type: sqlite
              path: 'test.db'
            postgres:
              type: postgres
              path: 'dbname=postgres user=postgres host=127.0.0.1'
              read_only: true
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        DuckDBConnectionConfig
    )
    from sqlmesh.core.config.connection import DuckDBAttachOptions

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect=<dialect>),
        gateways={
            "my_gateway": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    catalogs={
                        "memory": ":memory:",
                        "sqlite": DuckDBAttachOptions(
                            type="sqlite",
                            path="test.db"
                        ),
                        "postgres": DuckDBAttachOptions(
                            type="postgres",
                            path="dbname=postgres user=postgres host=127.0.0.1",
                            read_only=True
                        ),
                    }
                )
            ),
        }
    )
    ```

##### Catalogs for PostgreSQL

In PostgreSQL, the catalog name must match the actual catalog name it is associated with, as shown in the example above, where the database name (`dbname` in the path) is the same as the catalog name.

##### Connectors without schemas

Some connections, like SQLite, do not support schema names and therefore objects will be attached under the default schema name of `main`.

Example: mounting a SQLite database with the name `sqlite` that has a table `example_table` will be accessible as `sqlite.main.example_table`.

##### Sensitive fields in paths

If a connector, like Postgres, requires sensitive information in the path, it might support defining environment variables instead.
[See DuckDB Documentation for more information](https://duckdb.org/docs/extensions/postgres#configuring-via-environment-variables).

#### Cloud service authentication

DuckDB can read data directly from cloud services via extensions (e.g., [httpfs](https://duckdb.org/docs/extensions/httpfs/s3api), [azure](https://duckdb.org/docs/extensions/azure)).

The `secrets` option allows you to configure DuckDB's [Secrets Manager](https://duckdb.org/docs/configuration/secrets_manager.html) to authenticate with external services like S3. This is the recommended approach for cloud storage authentication in DuckDB v0.10.0 and newer, replacing the [legacy authentication method](https://duckdb.org/docs/stable/extensions/httpfs/s3api_legacy_authentication.html) via variables.

##### Secrets Configuration

The `secrets` option supports two formats:

1. **List format** (default secrets): A list of secret configurations where each secret uses DuckDB's default naming
2. **Dictionary format** (named secrets): A dictionary where keys are custom secret names and values are the secret configurations

This flexibility allows you to organize multiple secrets of the same type or reference specific secrets by name in your SQL queries.

##### List Format Example (Default Secrets)

Using a list creates secrets with DuckDB's default naming:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      duckdb:
        connection:
          type: duckdb
          catalogs:
            local: local.db
            remote: "s3://bucket/data/remote.duckdb"
          extensions:
            - name: httpfs
          secrets:
            - type: s3
              region: "YOUR_AWS_REGION"
              key_id: "YOUR_AWS_ACCESS_KEY"
              secret: "YOUR_AWS_SECRET_KEY"
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        DuckDBConnectionConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        gateways={
            "duckdb": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    catalogs={
                        "local": "local.db",
                        "remote": "s3://bucket/data/remote.duckdb"
                    },
                    extensions=[
                        {"name": "httpfs"},
                    ],
                    secrets=[
                        {
                            "type": "s3",
                            "region": "YOUR_AWS_REGION",
                            "key_id": "YOUR_AWS_ACCESS_KEY",
                            "secret": "YOUR_AWS_SECRET_KEY"
                        }
                    ]
                )
            ),
        }
    )
    ```

##### Dictionary Format Example (Named Secrets)

Using a dictionary allows you to assign custom names to your secrets for better organization and reference:

=== "YAML"

    ```yaml linenums="1"
    gateways:
      duckdb:
        connection:
          type: duckdb
          catalogs:
            local: local.db
            remote: "s3://bucket/data/remote.duckdb"
          extensions:
            - name: httpfs
          secrets:
            my_s3_secret:
              type: s3
              region: "YOUR_AWS_REGION"
              key_id: "YOUR_AWS_ACCESS_KEY"
              secret: "YOUR_AWS_SECRET_KEY"
            my_azure_secret:
              type: azure
              account_name: "YOUR_AZURE_ACCOUNT"
              account_key: "YOUR_AZURE_KEY"
    ```

=== "Python"

    ```python linenums="1"
    from sqlmesh.core.config import (
        Config,
        ModelDefaultsConfig,
        GatewayConfig,
        DuckDBConnectionConfig
    )

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        gateways={
            "duckdb": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    catalogs={
                        "local": "local.db",
                        "remote": "s3://bucket/data/remote.duckdb"
                    },
                    extensions=[
                        {"name": "httpfs"},
                    ],
                    secrets={
                        "my_s3_secret": {
                            "type": "s3",
                            "region": "YOUR_AWS_REGION",
                            "key_id": "YOUR_AWS_ACCESS_KEY",
                            "secret": "YOUR_AWS_SECRET_KEY"
                        },
                        "my_azure_secret": {
                            "type": "azure",
                            "account_name": "YOUR_AZURE_ACCOUNT",
                            "account_key": "YOUR_AZURE_KEY"
                        }
                    }
                )
            ),
        }
    )
    ```

After configuring the secrets, you can directly reference S3 paths in your catalogs or in SQL queries without additional authentication steps.

Refer to the official DuckDB documentation for the full list of [supported S3 secret parameters](https://duckdb.org/docs/stable/extensions/httpfs/s3api.html#overview-of-s3-secret-parameters) and for more information on the [Secrets Manager configuration](https://duckdb.org/docs/configuration/secrets_manager.html).

> Note: Loading credentials at runtime using `load_aws_credentials()` or similar deprecated functions may fail when using SQLMesh.

##### File system configuration example for Microsoft Onelake

The `filesystems` accepts a list of file systems to register in the DuckDB connection. This is especially useful for Azure Storage Accounts, as it adds write support for DuckDB which is not natively supported by DuckDB (yet).


=== "YAML"

    ```yaml linenums="1"
    gateways:
      ducklake:
        connection:
          type: duckdb
          catalogs:
            ducklake:
              type: ducklake
              path: myducklakecatalog.duckdb
              data_path: abfs://MyFabricWorkspace/MyFabricLakehouse.Lakehouse/Files/DuckLake.Files
              override_data_path: False
        extensions:
          - ducklake
        filesystems:
          - fs: abfs
            account_name: onelake
            account_host: onelake.blob.fabric.microsoft.com
            client_id: {{ env_var('AZURE_CLIENT_ID') }}
            client_secret: {{ env_var('AZURE_CLIENT_SECRET') }}
            tenant_id: {{ env_var('AZURE_TENANT_ID') }}
            # anon: False # To use azure.identity.DefaultAzureCredential authentication 
    ```


Refer to the documentation for `fsspec` [fsspec.filesystem](https://filesystem-spec.readthedocs.io/en/latest/api.html#fsspec.filesystem) and `adlfs` [adlfs.AzureBlobFileSystem](https://fsspec.github.io/adlfs/api/#api-reference) for a full list of storage options. 
