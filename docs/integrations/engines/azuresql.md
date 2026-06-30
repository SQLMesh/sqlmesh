# Azure SQL

[Azure SQL](https://azure.microsoft.com/en-us/products/azure-sql) is "a family of managed, secure, and intelligent products that use the SQL Server database engine in the Azure cloud."

## Local/Built-in Scheduler
**Engine Adapter Type**: `azuresql`

### Installation
#### User / Password Authentication:
```
pip install "sqlmesh[azuresql]"
```
#### Microsoft Entra ID / Azure Active Directory Authentication:
```
pip install "sqlmesh[azuresql-odbc]"
```
Set `driver: "pyodbc"` in your connection options.


#### Python Driver (Official Microsoft driver for Azure SQL):
See [`mssql-python`](https://pypi.org/project/mssql-python/) for more information.

```
pip install "sqlmesh[azuresql-mssql-python]"
```

Set `driver: "mssql-python"` in your connection options. This driver supports
[Entra ID auth](https://github.com/microsoft/mssql-python/wiki/Microsoft-Entra-ID-support),
for detailed connection options see [this link](https://github.com/microsoft/mssql-python/wiki/Connection-to-SQL-Database).  

!!! note
    The `mssql-python` driver [requires](https://pypi.org/project/mssql-python/) `python >= 3.10`.


### Connection options

| Option            | Description                                                                                                                                                                                                                                                                                                                                                            |     Type     | Required |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------: | :------: |
| `type`            | Engine type name - must be `azuresql`                                                                                                                                                                                                                                                                                                                                  |    string    |    Y     |
| `host`            | The hostname of the Azure SQL server                                                                                                                                                                                                                                                                                                                                   |    string    |    Y     |
| `user`            | The username / client ID to use for authentication with the Azure SQL server                                                                                                                                                                                                                                                                                           |    string    |    N     |
| `password`        | The password / client secret to use for authentication with the Azure SQL server                                                                                                                                                                                                                                                                                       |    string    |    N     |
| `port`            | The port number of the Azure SQL server                                                                                                                                                                                                                                                                                                                                |     int      |    N     |
| `database`        | The target database                                                                                                                                                                                                                                                                                                                                                    |    string    |    N     |
| `charset`         | The character set used for the connection                                                                                                                                                                                                                                                                                                                              |    string    |    N     |
| `timeout`         | The query timeout in seconds. Default: no timeout                                                                                                                                                                                                                                                                                                                      |     int      |    N     |
| `login_timeout`   | The timeout for connection and login in seconds. Default: 60                                                                                                                                                                                                                                                                                                           |     int      |    N     |
| `login_attempts`  | The number of reconnection attempts before failing. Default: 1 <br><br>*This option only applies to the `mssql-python` driver.                                                                                                                                                                                                                                         |     int      |    N     |
| `appname`         | The application name to use for the connection                                                                                                                                                                                                                                                                                                                         |    string    |    N     |
| `conn_properties` | The list of connection properties                                                                                                                                                                                                                                                                                                                                      | list[string] |    N     |
| `autocommit`      | Is autocommit mode enabled. Default: false                                                                                                                                                                                                                                                                                                                             |     bool     |    N     |
| `driver`          | The driver to use for the connection. Default: pymssql                                                                                                                                                                                                                                                                                                                 |    string    |    N     |
| `driver_name`     | The driver name to use for the connection (e.g., *ODBC Driver 18 for SQL Server*).                                                                                                                                                                                                                                                                                     |    string    |    N     |
| `odbc_properties` | The dict of ODBC connection properties (e.g., *authentication: ActiveDirectoryServicePrincipal*). See more [here](https://learn.microsoft.com/en-us/sql/connect/odbc/dsn-connection-string-attribute?view=sql-server-ver16).<br><br>*For the `mssql-python` driver, please see [this link](https://github.com/microsoft/mssql-python/wiki/Connection-to-SQL-Database). |     dict     |    N     |