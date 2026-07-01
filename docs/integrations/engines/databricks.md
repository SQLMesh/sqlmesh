# Databricks

This page provides information about how to use SQLMesh with the Databricks SQL engine. It begins with a description of the three methods for connecting SQLMesh to Databricks.

After that is a [Connection Quickstart](#connection-quickstart) that demonstrates how to connect to Databricks, or you can skip directly to information about using Databricks with the [built-in](#localbuilt-in-scheduler).

## Databricks connection methods

Databricks provides multiple computing options and connection methods. This section describes the three methods for connecting with SQLMesh.

### Databricks SQL Connector

SQLMesh connects to Databricks with the [Databricks SQL Connector](https://docs.databricks.com/dev-tools/python-sql-connector.html) library by default.

The SQL Connector is bundled with SQLMesh and automatically installed when you include the `databricks` extra in the command `pip install "sqlmesh[databricks]"`.

The SQL Connector has all the functionality needed for SQLMesh to execute SQL models on Databricks and Python models that do not return PySpark DataFrames.

If you have Python models returning PySpark DataFrames, check out the [Databricks Connect](#databricks-connect-1) section.

### Databricks Connect

If you want Databricks to process PySpark DataFrames in SQLMesh Python models, then SQLMesh must use the [Databricks Connect](https://docs.databricks.com/dev-tools/databricks-connect.html) library to connect to Databricks (instead of the Databricks SQL Connector library).

SQLMesh **DOES NOT** include/bundle the Databricks Connect library. You must [install the version of Databricks Connect](https://docs.databricks.com/en/dev-tools/databricks-connect/python/install.html) that matches the Databricks Runtime used in your Databricks cluster.

Find [more configuration details below](#databricks-connect-1).

### Databricks notebook interface

If you are always running SQLMesh commands directly in a Databricks Cluster interface (like in a Databricks Notebook using the [notebook magic commands](../../reference/notebook.md)), the SparkSession provided by Databricks is used to execute all SQLMesh commands.

Find [more configuration details below](#databricks-notebook-interface-1).

## Connection quickstart

Connecting to cloud warehouses involves a few steps, so this connection quickstart provides the info you need to get up and running with Databricks.

It demonstrates connecting to a Databricks [All-Purpose Compute](https://docs.databricks.com/en/compute/index.html) instance with the `databricks-sql-connector` Python library bundled with SQLMesh.

!!! tip
    This quickstart assumes you are familiar with basic SQLMesh commands and functionality.

    If you're not, work through the [SQLMesh Quickstart](../../quick_start.md) before continuing!

### Prerequisites

Before working through this connection quickstart, ensure that:

1. You have a Databricks account with access to an appropriate Databricks Workspace
    - The Workspace must support authenticating with [personal access tokens](https://docs.databricks.com/en/dev-tools/auth/pat.html) (Databricks [Community Edition workspaces do not](https://docs.databricks.com/en/admin/access-control/tokens.html))
    - Your account must have Workspace Access and Create Compute permissions (these permissions are enabled by default)
2. Your Databricks compute resources have [Unity Catalog](https://docs.databricks.com/aws/en/data-governance/unity-catalog/) activated
3. Your computer has [SQLMesh installed](../../installation.md) with the [Databricks extra available](../../installation.md#install-extras)
    - Install from the command line with the command `pip install "sqlmesh[databricks]"`
4. You have initialized a [SQLMesh example project](../../quickstart/cli#1-create-the-sqlmesh-project) on your computer
    - Open a command line interface and navigate to the directory where the project files should go
    - Initialize the project with the command `sqlmesh init duckdb`

!!! important "Unity Catalog required"

    Databricks compute resources used by SQLMesh must have [Unity Catalog](https://docs.databricks.com/aws/en/data-governance/unity-catalog/) activated.

### Get connection info

The first step to configuring a Databricks connection is gathering the necessary information from your Databricks compute instance.

#### Create Compute

We must have something to connect to, so we first create and activate a Databricks compute instance. If you already have one running, skip to the [next section](#get-jdbcodbc-info).

We begin in the default view for our Databricks Workspace. Access the Compute view by clicking the `Compute` entry in the left-hand menu:

![Databricks Workspace default view](./databricks/db-guide_workspace.png){ loading=lazy }

In the Compute view, click the `Create compute` button:

![Databricks Compute default view](./databricks/db-guide_compute.png){ loading=lazy }

Modify compute cluster options if desired and click the `Create compute` button:

![Databricks Create Compute view](./databricks/db-guide_compute-create.png){ loading=lazy }

#### Get JDBC/ODBC info

Scroll to the bottom of the view and click the open the `Advanced Options` view:

![Databricks Compute Advanced Options link](./databricks/db-guide_compute-advanced-options-link.png){ loading=lazy }

Click the `JDBC/ODBC` tab:

![Databricks Compute Advanced Options JDBC/ODBC tab](./databricks/db-guide_advanced-options.png){ loading=lazy }

Open your project's `config.yaml` configuration file in a text editor and add a new gateway named `databricks` below the existing `local` gateway:

![Project config.yaml databricks gateway](./databricks/db-guide_config-yaml.png){ loading=lazy }

Copy the `server_hostname` and `http_path` connection values from the Databricks JDBC/ODBC tab to the `config.yaml` file:

![Copy server_hostname and http_path to config.yaml](./databricks/db-guide_copy-server-http.png){ loading=lazy }

#### Get personal access token

The final piece of information we need for the `config.yaml` file is your personal access token.

!!! warning
    **Do not share your personal access token with anyone.**

    Best practice for storing secrets like access tokens is placing them in [environment variables that the configuration file loads dynamically](../../guides/configuration.md#environment-variables). For simplicity, this guide instead places the value directly in the configuration file.

    This code demonstrates how to use the environment variable `DATABRICKS_ACCESS_TOKEN` for the configuration's `access_token` parameter:

    ```yaml linenums="1"
    gateways:
      databricks:
          connection:
            type: databricks
            access_token: {{ env_var('DATABRICKS_ACCESS_TOKEN') }}
    ```

<br></br>
To create a personal access token, click on your profile logo and go to your profile's `Settings` page:

![Navigate to profile Settings page](./databricks/db-guide_profile-settings-link.png){ loading=lazy }

Go to the `Developer` view in the User menu. Depending on your account's role, your page may not display the Workspace Admin section of the page.

![Navigate to User Developer view](./databricks/db-guide_profile-settings-developer.png){ loading=lazy }

Click the `Manage` button in the Access Tokens section:

![Navigate to Access Tokens management](./databricks/db-guide_access-tokens-link.png){ loading=lazy }

Click the `Generate new token` button:

![Open the token generation menu](./databricks/db-guide_access-tokens-generate-button.png){ loading=lazy }

Name your token in the `Comment` field, and click the `Generate` button:

![Generate a new token](./databricks/db-guide_access-tokens-generate.png){ loading=lazy }

Click the copy button and paste the token into the `access_token` key:

![Copy token to config.yaml access_token key](./databricks/db-guide_copy-token.png){ loading=lazy }

!!! warning
    **Do not share your personal access token with anyone.**

    Best practice for storing secrets like access tokens is placing them in [environment variables that the configuration file loads dynamically](../../guides/configuration.md#environment-variables). For simplicity, this guide instead places the value directly in the configuration file.

    This code demonstrates how to use the environment variable `DATABRICKS_ACCESS_TOKEN` for the configuration's `access_token` parameter:

    ```yaml linenums="1"
    gateways:
      databricks:
          connection:
            type: databricks
            access_token: {{ env_var('DATABRICKS_ACCESS_TOKEN') }}
    ```

### Check connection

We have now specified the `databricks` gateway connection information, so we can confirm that SQLMesh is able to successfully connect to Databricks. We will test the connection with the `sqlmesh info` command.

First, open a command line terminal. Now enter the command `sqlmesh --gateway databricks info`.

We manually specify the `databricks` gateway because it is not our project's default gateway:

![Run sqlmesh info command in CLI](./databricks/db-guide_sqlmesh-info.png){ loading=lazy }

The output shows that our data warehouse connection succeeded:

![Successful data warehouse connection](./databricks/db-guide_sqlmesh-info-succeeded.png){ loading=lazy }

However, the output includes a `WARNING` about using the Databricks SQL engine for storing SQLMesh state:

![Databricks state connection warning](./databricks/db-guide_sqlmesh-info-warning.png){ loading=lazy }

!!! warning
    Databricks is not designed for transactional workloads and should not be used to store SQLMesh state even in testing deployments.

    Learn more about storing SQLMesh state [here](../../guides/configuration.md#state-connection).

### Specify state connection

We can store SQLMesh state in a different SQL engine by specifying a `state_connection` in our `databricks` gateway.

This example uses the DuckDB engine to store state in the local `databricks_state.db` file:

![Specify DuckDB state connection](./databricks/db-guide_state-connection.png){ loading=lazy }

Now we no longer see the warning when running `sqlmesh --gateway databricks info`, and we see a new entry `State backend connection succeeded`:

![No state connection warning](./databricks/db-guide_sqlmesh-info-no-warning.png){ loading=lazy }

### Run a `sqlmesh plan`

For convenience, we can omit the `--gateway` option from our CLI commands by specifying `databricks` as our project's `default_gateway`:

![Specify databricks as default gateway](./databricks/db-guide_default-gateway.png){ loading=lazy }

And run a `sqlmesh plan` in Databricks:

![Run sqlmesh plan in databricks](./databricks/db-guide_sqlmesh-plan.png){ loading=lazy }

And confirm that our schemas and objects exist in the Databricks catalog:

![Sqlmesh plan objects in databricks](./databricks/db-guide_sqlmesh-plan-objects.png){ loading=lazy }

Congratulations - your SQLMesh project is up and running on Databricks!

!!! tip
    SQLMesh connects to your Databricks Cluster's default catalog by default. Connect to a different catalog by specifying its name in the connection configuration's [`catalog` parameter](#connection-options).

## Local/Built-in Scheduler
**Engine Adapter Type**: `databricks`

### Installation
```
pip install "sqlmesh[databricks]"
```

### Connection method details

Databricks provides multiple computing options and connection methods. The [section above](#databricks-connection-methods) explains how to use them with SQLMesh, and this section provides additional configuration details.

#### Databricks SQL Connector

SQLMesh uses the [Databricks SQL Connector](https://docs.databricks.com/dev-tools/python-sql-connector.html) to connect to Databricks by default. Learn [more above](#databricks-sql-connector).

#### Databricks Connect

If you want Databricks to process PySpark DataFrames in SQLMesh Python models, then SQLMesh needs to use the [Databricks Connect](https://docs.databricks.com/dev-tools/databricks-connect.html) to connect to Databricks (instead of the Databricks SQL Connector).

SQLMesh **DOES NOT** include/bundle the Databricks Connect library. You must [install the version of Databricks Connect](https://docs.databricks.com/en/dev-tools/databricks-connect/python/install.html) that matches the Databricks Runtime used in your Databricks cluster.

If SQLMesh detects that you have Databricks Connect installed, then it will automatically configure the connection and use it for all Python models that return a Pandas or PySpark DataFrame.

To have databricks-connect installed but ignored by SQLMesh, set `disable_databricks_connect` to `true` in the connection configuration.

Databricks Connect can execute SQL and DataFrame operations on different clusters by setting the SQLMesh `databricks_connect_*` connection options. For example, these options could configure SQLMesh to run SQL on a [Databricks SQL Warehouse](https://docs.databricks.com/sql/admin/create-sql-warehouse.html) while still routing DataFrame operations to a normal Databricks Cluster.

!!! note
    If using Databricks Connect, make sure to learn about the Databricks [requirements](https://docs.databricks.com/dev-tools/databricks-connect.html#requirements) and [limitations](https://docs.databricks.com/dev-tools/databricks-connect.html#limitations).

#### Databricks notebook interface

If you are always running SQLMesh commands directly on a Databricks Cluster (like in a Databricks Notebook using the [notebook magic commands](../../reference/notebook.md)), the SparkSession provided by Databricks is used to execute all SQLMesh commands.

The only relevant SQLMesh configuration parameter is the optional `catalog` parameter.

### Connection options

| Option                               | Description                                                                                                                                                                                                                                         |  Type  | Required |
|--------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:------:|:--------:|
| `type`                               | Engine type name - must be `databricks`                                                                                                                                                                                                             | string |    Y     |
| `server_hostname`                    | Databricks instance host name                                                                                                                                                                                                                       | string |    N     |
| `http_path`                          | HTTP path, either to a DBSQL endpoint (such as `/sql/1.0/endpoints/1234567890abcdef`) or to an All-Purpose cluster (such as `/sql/protocolv1/o/1234567890123456/1234-123456-slid123`)                                                               | string |    N     |
| `access_token`                       | HTTP Bearer access token, such as Databricks Personal Access Token                                                                                                                                                                                  | string |    N     |
| `catalog`                            | The name of the catalog to use for the connection. [Defaults to use Databricks cluster default](https://docs.databricks.com/en/data-governance/unity-catalog/create-catalogs.html#the-default-catalog-configuration-when-unity-catalog-is-enabled). | string |    N     |
| `auth_type`                          | SQL Connector Only: Set to 'databricks-oauth' or 'azure-oauth' to trigger OAuth (or dont set at all to use `access_token`)                                                                                                                          | string |    N     |
| `oauth_client_id`                    | SQL Connector Only: Optional [M2M](https://docs.databricks.com/en/dev-tools/python-sql-connector.html#oauth-machine-to-machine-m2m-authentication) OAuth Client ID to use when `auth_type` is set                                                   | string |    N     |
| `oauth_client_secret`                | SQL Connector Only: Optional [M2M](https://docs.databricks.com/en/dev-tools/python-sql-connector.html#oauth-machine-to-machine-m2m-authentication) OAuth Client Secret to use when `auth_type` is set                                               | string |    N     |
| `http_headers`                       | SQL Connector Only: An optional dictionary of HTTP headers that will be set on every request                                                                                                                                                        |  dict  |    N     |
| `session_configuration`              | SQL Connector Only: An optional dictionary of Spark session parameters. Execute the SQL command `SET -v` to get a full list of available commands.                                                                                                  |  dict  |    N     |
| `databricks_connect_server_hostname` | Databricks Connect Only: Databricks Connect server hostname. Uses `server_hostname` if not set.                                                                                                                                                     | string |    N     |
| `databricks_connect_access_token`    | Databricks Connect Only: Databricks Connect access token. Uses `access_token` if not set.                                                                                                                                                           | string |    N     |
| `databricks_connect_cluster_id`      | Databricks Connect Only: Databricks Connect cluster ID. Uses `http_path` if not set. Cannot be a Databricks SQL Warehouse.                                                                                                                          | string |    N     |
| `databricks_connect_use_serverless`  | Databricks Connect Only: Use a serverless cluster for Databricks Connect instead of `databricks_connect_cluster_id`.                                                                                                                                |  bool  |    N     |
| `force_databricks_connect`           | When running locally, force the use of Databricks Connect for all model operations (so don't use SQL Connector for SQL models)                                                                                                                      |  bool  |    N     |
| `disable_databricks_connect`         | When running locally, disable the use of Databricks Connect for all model operations (so use SQL Connector for all models)                                                                                                                          |  bool  |    N     |
| `disable_spark_session`              | Do not use SparkSession if it is available (like when running in a notebook).                                                                                                                                                                       |  bool  |    N     |

### Query tags

Databricks SQL Connector supports per-query tags through the `query_tags` model session property. Specify tags as a `MAP(...)` of string keys to string or `NULL` values:

```sql
MODEL (
    name sqlmesh_example.tagged_model,
    dialect databricks,
    session_properties (
        query_tags = MAP(
            'team', 'data-eng',
            'app', 'sqlmesh',
            'feature', NULL
        )
    )
);

SELECT 1 AS id;
```

Query tags are only applied when SQLMesh executes SQL through the Databricks SQL Connector. They are not applied when SQLMesh routes execution through Databricks Connect, a Databricks notebook SparkSession, or the Spark engine adapter.

## Model table properties to support altering tables

If you are making a change to the structure of a table that is [forward only](../../guides/incremental_time.md#forward-only-models), then you may need to add the following to your model's `physical_properties`:


```sql
MODEL (
    name sqlmesh_example.new_model,
    ...
    physical_properties (
        'delta.columnMapping.mode' = 'name'
    ),
)
```

If you attempt to alter without having this property set, you will get an error similar to `databricks.sql.exc.ServerOperationError: [DELTA_UNSUPPORTED_DROP_COLUMN] DROP COLUMN is not supported for your Delta table.`.
[Databricks Documentation for more details](https://docs.databricks.com/en/delta/column-mapping.html#requirements).

## Liquid Clustering

SQLMesh supports the liquid clustering keywords AUTO and NONE

```sql
MODEL (
    name sqlmesh_example.new_model,
    ...
    clustered_by AUTO
)
```

To cluster by a column called `auto` or `none`, use parentheses and backticks

```sql
MODEL (
    name sqlmesh_example.new_model,
    ...
    clustered_by (`auto`)
)
```
