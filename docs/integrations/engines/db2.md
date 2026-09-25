# Db2

This page provides information about how to use SQLMesh with [IBM Db2](https://www.ibm.com/products/db2).

!!! info
    The Db2 engine adapter is a community contribution. Due to this, only limited community support is available.

## Local/Built-in Scheduler

**Engine Adapter Type**: `db2`

### Installation

```
pip install "sqlmesh[db2]"
```

### Connection options

| Option              | Description                                                                                    | Type   | Required |
|---------------------|------------------------------------------------------------------------------------------------|:------:|:--------:|
| `type`              | Engine type name - must be `db2`                                                               | string | Y        |
| `host`              | The hostname of the Db2 server                                                                 | string | Y        |
| `port`              | The port number of the Db2 server. Default: `50000`                                            | int    | N        |
| `database`          | The name of the Db2 database to connect to                                                     | string | Y        |
| `username`          | The username to use for authentication with the Db2 server                                     | string | Y        |
| `password`          | The password to use for authentication with the Db2 server                                     | string | Y        |
| `db2_schema`        | Sets `CURRENTSCHEMA` on the connection. Controls the default schema for unqualified references. Typically set to the same value as `username`. | string | Y        |
| `ssl`               | Enable TLS/SSL encryption. Default: `false`                                                    | bool   | N        |
| `connect_timeout`   | The number of seconds to wait for the connection to the server. Default: `30`                  | int    | N        |
| `concurrent_tasks`  | Maximum number of tasks to run concurrently. Default: `4`                                      | int    | N        |

## Important Notes

**State connection:** Db2 is **not supported** as a SQLMesh `state_connection`. Use DuckDB (recommended) or another supported engine for SQLMesh state storage:

```yaml linenums="1"
gateways:
  db2:
    connection:
      type: db2
      host: localhost
      port: 50000
      database: TESTDB
      username: db2inst1
      password: your_password
      db2_schema: db2inst1
    state_connection:
      type: duckdb
      database: ./state/sqlmesh_state.db

default_gateway: db2

model_defaults:
  dialect: db2
```

**Table naming:** Db2 rejects table names that start with an underscore (`_`). SQLMesh's default physical table naming convention can generate names beginning with `_`. To avoid this, set `physical_table_naming_convention` to `hash_md5` in your project config:

```yaml
physical_table_naming_convention: hash_md5
```

## Limitations

- **Single catalog only**: Db2 operates in single-catalog mode; cross-catalog queries are not supported.
- **No inline column comments**: Column-level comments cannot be set inline during table creation.
- **No atomic table replacement**: Db2 does not support `CREATE OR REPLACE TABLE`, so full model refreshes are not atomic. There is a brief window during which the table may be empty or partially populated.
- **Identifier length**: Maximum identifier length is 128 characters.
- **No `SELECT ... FOR UPDATE`**: Db2 does not support `SELECT ... FOR UPDATE` in the same way as OLTP databases; SQLMesh removes this clause when executing queries.

## Resources

- [IBM Db2 Documentation](https://www.ibm.com/docs/en/db2)
- [IBM Db2 SQL Reference](https://www.ibm.com/docs/en/db2/11.5?topic=db2-sql)
