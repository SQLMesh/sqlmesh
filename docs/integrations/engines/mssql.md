# MSSQL

## Installation

### User / Password Authentication:
```
pip install "sqlmesh[mssql]"
```

### Microsoft Entra ID / Azure Active Directory Authentication:
```
pip install "sqlmesh[mssql-odbc]"
```
Set `driver: "pyodbc"` in your connection options.

#### Python Driver (Official Microsoft driver for Azure SQL):
See [`mssql-python`](https://pypi.org/project/mssql-python/) for more information.
```
pip install "sqlmesh[mssql-python]"
```
Set `driver: "mssql-python"` in your connection options.


## Incremental by unique key `MERGE`

SQLMesh executes a `MERGE` statement to insert rows for [incremental by unique key](../../concepts/models/model_kinds.md#incremental_by_unique_key) model kinds.

By default, the `MERGE` statement updates all non-key columns of an existing row when a new row with the same key values is inserted. If all column values match between the two rows, those updates are unnecessary.

SQLMesh provides an optional performance optimization that skips unnecessary updates by comparing column values with the `EXISTS` and `EXCEPT` operators.

Enable the optimization by setting the `mssql_merge_exists` key to `true` in the [`physical_properties`](../../concepts/models/overview.md#physical_properties) section of the `MODEL` statement.

For example:

```sql linenums="1" hl_lines="7-9"
MODEL (
    name sqlmesh_example.unique_key,
    kind INCREMENTAL_BY_UNIQUE_KEY (
        unique_key id
    ),
    cron '@daily',
    physical_properties (
        mssql_merge_exists = true
    )
);
```

!!! warning "Not all column types supported"
    The `mssql_merge_exists` optimization is not supported for all column types, including `GEOMETRY`, `XML`, `TEXT`, `NTEXT`, `IMAGE`, and most user-defined types.

    Learn more in the [MSSQL `EXCEPT` statement documentation](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/set-operators-except-and-intersect-transact-sql?view=sql-server-ver17#arguments).

## Local/Built-in Scheduler
**Engine Adapter Type**: `mssql`

### Connection options

| Option            | Description                                                                                                                                                                                                               |     Type     | Required |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------: | :------: |
| `type`            | Engine type name - must be `mssql`                                                                                                                                                                                        |    string    |    Y     |
| `host`            | The hostname of the MSSQL server                                                                                                                                                                                          |    string    |    Y     |
| `user`            | The username / client id to use for authentication with the MSSQL server                                                                                                                                                  |    string    |    N     |
| `password`        | The password / client secret to use for authentication with the MSSQL server                                                                                                                                              |    string    |    N     |
| `port`            | The port number of the MSSQL server                                                                                                                                                                                       |     int      |    N     |
| `database`        | The target database                                                                                                                                                                                                       |    string    |    N     |
| `charset`         | The character set used for the connection                                                                                                                                                                                 |    string    |    N     |
| `timeout`         | The query timeout in seconds. Default: no timeout                                                                                                                                                                         |     int      |    N     |
| `login_timeout`   | The timeout for connection and login in seconds. Default: 60                                                                                                                                                              |     int      |    N     |
| `appname`         | The application name to use for the connection                                                                                                                                                                            |    string    |    N     |
| `conn_properties` | The list of connection properties                                                                                                                                                                                         | list[string] |    N     |
| `autocommit`      | Is autocommit mode enabled. Default: false                                                                                                                                                                                |     bool     |    N     |
| `driver`          | The driver to use for the connection. Default: pymssql                                                                                                                                                                    |    string    |    N     |
| `driver_name`     | The driver name to use for the connection (e.g., *ODBC Driver 18 for SQL Server*).                                                                                                                                          |    string    |    N     |
| `odbc_properties` | ODBC connection properties (e.g., *authentication: ActiveDirectoryServicePrincipal*). See more [here](https://learn.microsoft.com/en-us/sql/connect/odbc/dsn-connection-string-attribute?view=sql-server-ver16). |     dict     |    N     |