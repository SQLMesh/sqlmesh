# Postgres

## Local/Built-in Scheduler
**Engine Adapter Type**: `postgres`

### Installation
```
pip install "sqlmesh[postgres]"
```

### Connection options

| Option             | Description                                                                     | Type   | Required |
|--------------------|---------------------------------------------------------------------------------|:------:|:--------:|
| `type`             | Engine type name - must be `postgres`                                           | string | Y        |
| `host`             | The hostname of the Postgres server                                             | string | Y        |
| `user`             | The username to use for authentication with the Postgres server                 | string | Y        |
| `password`         | The password to use for authentication with the Postgres server                 | string | Y        |
| `port`             | The port number of the Postgres server                                          | int    | Y        |
| `database`         | The name of the database instance to connect to                                 | string | Y        |
| `keepalives_idle`  | The number of seconds between each keepalive packet sent to the server.         | int    | N        |
| `connect_timeout`  | The number of seconds to wait for the connection to the server. (Default: `10`) | int    | N        |
| `role`             | The role to use for authentication with the Postgres server                     | string | N        |
| `sslmode`          | The security of the connection to the Postgres server                           | string | N        |
| `application_name` | The name of the application to use for the connection                           | string | N        |

### DataFrame loading

When using the `psycopg2` driver, SQLMesh loads pandas DataFrames (such as those returned by Python models and used for seeds) with `COPY ... FROM STDIN (FORMAT csv)` instead of batched `INSERT ... VALUES` statements, which is much faster for large DataFrames. The data is copied into a temporary staging table, which is created in the same schema as the target table and then cast into the target table's column types. The staging table is dropped afterwards, including when the load fails. Because it is created in the target's schema, the connecting user needs the `CREATE` privilege on that schema, as for other SQLMesh DDL.

No configuration is required and there is no option to disable this behavior. SQLMesh automatically falls back to `INSERT ... VALUES` when:

- A column has a type or values that cannot be loaded with `COPY` identically. Supported columns are integers (up to 64 bits signed, up to 32 bits unsigned), `float64`, booleans, timestamps (with or without time zone), and strings. Other types, such as `float32`, 64-bit unsigned integers, `Decimal`, dates, bytes, dicts, lists, and categoricals, trigger the fallback.
- The DataFrame has a single column and one of its values is `\.`.
- The driver does not support `copy_expert` (for example, a driver other than `psycopg2`).

The RisingWave engine always uses `INSERT ... VALUES`.
