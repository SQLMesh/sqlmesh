# Trino

## Local/Built-in Scheduler
**Engine Adapter Type**: `trino`

NOTE: Trino may not be used for the SQLMesh [state connection](../../reference/configuration.md#connections).

## Installation
```
pip install "sqlmesh[trino]"
```

If you are using Oauth for Authentication, it is recommended to install keyring cache:
```
pip install "trino[external-authentication-token-cache]"
```

### Trino Connector Support

The trino engine adapter has been tested against the [Hive Connector](https://trino.io/docs/current/connector/hive.html), [Iceberg Connector](https://trino.io/docs/current/connector/iceberg.html), and [Delta Lake Connector](https://trino.io/docs/current/connector/delta-lake.html).

Please let us know on [Slack](https://tobikodata.com/slack) if you are wanting to use another connector or have tried another connector.

#### Hive Connector Configuration

Recommended hive catalog properties configuration (`<catalog_name>.properties`):

```properties linenums="1"
hive.metastore-cache-ttl=0s
hive.metastore-refresh-interval=5s
hive.metastore-timeout=10s
hive.allow-drop-table=true
hive.allow-add-column=true
hive.allow-drop-column=true
hive.allow-rename-column=true
hive.allow-rename-table=true
```

#### Iceberg Connector Configuration

If you're using a hive metastore for the Iceberg catalog, the [properties](https://trino.io/docs/current/connector/metastores.html#general-metastore-configuration-properties) are mostly the same as the Hive connector.

```properties linenums="1"
iceberg.catalog.type=hive_metastore
# metastore properties as per the Hive Connector Configuration above
```

**Note**: The Trino Iceberg Connector must be configured with an `iceberg.catalog.type` that supports views. At the time of this writing, this is `hive_metastore`, `glue`, and `rest`.

The `jdbc` and `nessie` iceberg catalog types do not support views and are thus incompatible with SQLMesh.

!!! info "Nessie"
    Nessie is supported when used as an Iceberg REST Catalog (`iceberg.catalog.type=rest`).
    For more information on how to configure the Trino Iceberg connector for this, see the [Nessie documentation](https://projectnessie.org/nessie-latest/trino/).

#### Delta Lake Connector Configuration

The Trino adapter Delta Lake connector has only been tested with the Hive metastore catalog type.

The [properties file](https://trino.io/docs/current/connector/delta-lake.html#general-configuration) must include the Hive metastore URI and catalog name in addition to any other [general properties](https://trino.io/docs/current/object-storage/metastores.html#general-metastore-properties).

``` properties linenums="1"
hive.metastore.uri=thrift://example.net:9083
delta.hive-catalog-name=datalake_delta # example catalog name, can be any valid string
```

#### AWS Glue

[AWS Glue](https://aws.amazon.com/glue/) provides an implementation of the Hive metastore catalog.

Your Trino project's physical data objects are stored in a specific location, such as an [AWS S3](https://aws.amazon.com/s3/) bucket. Hive provides a default location, which you can override in its configuration file.

Set the default location for your project's tables in the Hive catalog configuration's [`hive.metastore.glue.default-warehouse-dir` parameter](https://trino.io/docs/current/object-storage/metastores.html#aws-glue-catalog-configuration-properties).

For example:

```linenums="1"
hive.metastore=glue
hive.metastore.glue.default-warehouse-dir=s3://my-bucket/
```

### Connection options

| Option                    | Description                                                                                                                                                                             |  Type  | Required |
|---------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:------:|:--------:|
| `type`                    | Engine type name - must be `trino`                                                                                                                                                      | string |    Y     |
| `user`                    | The username (of the account) to log in to your cluster. When connecting to Starburst Galaxy clusters, you must include the role of the user as a suffix to the username.               | string |    Y     |
| `host`                    | The hostname of your cluster. Don't include the `http://` or `https://` prefix.                                                                                                         | string |    Y     |
| `catalog`                 | The name of a catalog in your cluster.                                                                                                                                                  | string |    Y     |
| `http_scheme`             | The HTTP scheme to use when connecting to your cluster. By default, it's `https` and can only be `http` for no-auth or basic auth.                                                      | string |    N     |
| `port`                    | The port to connect to your cluster. By default, it's `443` for `https` scheme and `80` for `http`                                                                                      |  int   |    N     |
| `roles`                   | Mapping of catalog name to a role                                                                                                                                                       |  dict  |    N     |
| `source`                  | Value to send as Trino's `source` field for query attribution / auditing. Default: `sqlmesh`.                                                                                           | string |    N     |
| `http_headers`            | Additional HTTP headers to send with each request.                                                                                                                                      |  dict  |    N     |
| `session_properties`      | Trino session properties. Run `SHOW SESSION` to see all options.                                                                                                                        |  dict  |    N     |
| `retries`                 | Number of retries to attempt when a request fails. Default: `3`                                                                                                                         |  int   |    N     |
| `timezone`                | Timezone to use for the connection. Default: client-side local timezone                                                                                                                 | string |    N     |
| `schema_location_mapping` | A mapping of regex patterns to S3 locations to use for the `LOCATION` property when creating schemas. See [Table and Schema locations](#table-and-schema-locations) for more details.   |  dict  |    N     |
| `catalog_type_overrides`  | A mapping of catalog names to their connector type. This is used to enable/disable connector specific behavior. See [Catalog Type Overrides](#catalog-type-overrides) for more details. |  dict  |    N     |

## Table and Schema locations

When using connectors that are decoupled from their storage (such as the Iceberg, Hive or Delta connectors), when creating new tables Trino needs to know the location in the physical storage it should write the table data to.

This location gets stored against the table in the metastore so that any engine trying to read the data knows where to look.

### Default behaviour

Trino allows you to optionally configure a `default-warehouse-dir` property at the [Metastore](https://trino.io/docs/current/object-storage/metastores.html) level. When creating objects, Trino will infer schema locations to be `<default warehouse dir>/<schema name>` and table locations to be `<default warehouse dir>/<schema name>/<table name>`.

However, if you dont set this property, Trino can still infer table locations if a *schema* location is explicitly set.

For example, if you specify the `LOCATION` property when creating a schema like so:

```sql
CREATE SCHEMA staging_data
WITH (LOCATION = 's3://warehouse/production/staging_data')
```

Then any tables created under that schema will have their location inferred as `<schema location>/<table name>`.

If you specify neither a `default-warehouse-dir` in the metastore config nor a schema location when creating the schema, you must specify an explicit table location when creating the table or Trino will produce an error.

Creating a table in a specific location is very similar to creating a schema in a specific location:

```sql
CREATE TABLE staging_data.customers (customer_id INT)
WITH (LOCATION = 's3://warehouse/production/staging_data/customers')
```

### Configuring in SQLMesh

Within SQLMesh, you can configure the value to use for the `LOCATION` property when SQLMesh creates tables and schemas. This overrides what Trino would have inferred based on the cluster configuration.

#### Schemas

To configure the `LOCATION` property that SQLMesh will specify when issuing `CREATE SCHEMA` statements, you can use the `schema_location_mapping` connection property. This applies to all schemas that SQLMesh creates, including its internal ones.

The simplest example is to emulate a `default-warehouse-dir`:

```yaml title="config.yaml"
gateways:
  trino:
    connection:
      type: trino
      ...
      schema_location_mapping:
        '.*': 's3://warehouse/production/@{schema_name}'
```

This will cause all schemas to get created with their location set to `s3://warehouse/production/<schema name>`. The table locations will be inferred by Trino as `s3://warehouse/production/<schema name>/<table name>` so all objects will effectively be created under `s3://warehouse/production/`.

It's worth mentioning that if your models are using fully qualified three part names, eg `<catalog>.<schema>.<name>` then string being matched against the `schema_location_mapping` regex will be `<catalog>.<schema>` and not just the `<schema>` itself. This allows you to set different locations for the same schema name if that schema name is used across multiple catalogs.

If your models are using two part names, eg `<schema>.<table>` then only the `<schema>` part will be matched against the regex.

Here's an example:

```yaml title="config.yaml"
gateways:
  trino:
    connection:
      type: trino
      ...
      schema_location_mapping:
        '^utils$': 's3://utils-bucket/@{schema_name}'
        '^landing\..*$': 's3://raw-data/@{catalog_name}/@{schema_name}'
        '^staging.*$': 's3://bucket/@{schema_name}_dev'
        '^sqlmesh.*$': 's3://sqlmesh-internal/dev/@{schema_name}'
```

This would perform the following mappings:

- a schema called `sales` would not be mapped to a location at all because it doesnt match any of the patterns. It would be created without a `LOCATION` property
- a schema called `utils` would be mapped to the location `s3://utils-bucket/utils` because it directly matches the `^utils$` pattern
- a schema called `transactions` in a catalog called `landing` would be mapped to the location `s3://raw-data/landing/transactions` because the string `landing.transactions` matches the `^landing\..*$` pattern
- schemas called `staging_customers` and `staging_accounts` would be mapped to the locations `s3://bucket/staging_customers_dev` and `s3://bucket/staging_accounts_dev` respectively because they match the `^staging.*$` pattern
- a schema called `accounts` in a catalog called `staging` would be mapped to the location `s3://bucket/accounts_dev` because the string `staging.accounts` matches the `^staging.*$` pattern
- schemas called `sqlmesh__staging_customers` and `sqlmesh__staging_utils` would be mapped to the locations `s3://sqlmesh-internal/dev/sqlmesh__staging_customers` and `s3://sqlmesh-internal/dev/sqlmesh__staging_utils` respectively because they match the `^sqlmesh.*$` pattern

!!! info "Placeholders"
    You may use the `@{catalog_name}` and `@{schema_name}` placeholders in the mapping value.

    If there is a match on one of the patterns then the catalog / schema that SQLMesh is about to use in the `CREATE SCHEMA` statement will be substituted into these placeholders.

    Note the use of curly brace syntax `@{}` when referencing these placeholders - learn more [here](../../concepts/macros/sqlmesh_macros.md#embedding-variables-in-strings).

#### Tables

Often, you don't need to configure an explicit table location because if you have configured explicit schema locations, table locations are automatically inferred by Trino to be a subdirectory under the schema location.

However, if you need to, you can configure an explicit table location by adding a `location` property to the model `physical_properties`.

Note that you need to use the [@resolve_template](../../concepts/macros/sqlmesh_macros.md#resolve_template) macro to generate a unique table location for each model version. Otherwise, all model versions will be written to the same location and clobber each other.

```sql hl_lines="5"
MODEL (
  name staging.customers,
  kind FULL,
  physical_properties (
    location = @resolve_template('s3://warehouse/@{catalog_name}/@{schema_name}/@{table_name}')
  )
);

SELECT ...
```

This will cause SQLMesh to set the specified `LOCATION` when issuing a `CREATE TABLE` statement.

## Catalog Type Overrides

SQLMesh attempts to determine the connector type of a catalog by querying the `system.metadata.catalogs` table and checking the `connector_name` column.
It checks if the connector name is `hive` for Hive connector behavior or contains `iceberg` or `delta_lake` for Iceberg or Delta Lake connector behavior respectively.
However, the connector name may not always be a reliable way to determine the connector type, for example when using a custom connector or a fork of an existing connector.
To handle such cases, you can use the `catalog_type_overrides` connection property to explicitly specify the connector type for specific catalogs.
For example, to specify that the `datalake` catalog is using the Iceberg connector and the `analytics` catalog is using the Hive connector, you can configure the connection as follows:

```yaml title="config.yaml"
gateways:
  trino:
    connection:
      type: trino
      ...
      catalog_type_overrides:
        datalake: iceberg
        analytics: hive
```

## Authentication

=== "No Auth"
    | Option     | Description                              |  Type  | Required |
    |------------|------------------------------------------|:------:|:--------:|
    | `method`   | `no-auth` (Default)                      | string |    N     |

    ```yaml linenums="1"
    gateway_name:
      connection:
        type: trino
        user: [user]
        host: [host]
        catalog: [catalog]
        # Most likely you will want http for scheme when not using auth
        http_scheme: http
    ```


=== "Basic Auth"

    | Option     | Description                                                                                                                                                                  |  Type  | Required |
    | ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----: | :------: |
    | `method`   | `basic`                                                                                                                                                                      | string |    Y     |
    | `password` | The password to use when authenticating.                                                                                                                                     | string |    Y     |
    | `verify`   | Boolean flag for whether SSL verification should occur. Default: [trinodb Python client](https://github.com/trinodb/trino-python-client) default (`true` as of this writing) |  bool  |    N     |


    ```yaml linenums="1"
    gateway_name:
      connection:
        type: trino
        method: basic
        user: [user]
        password: [password]
        host: [host]
        catalog: [catalog]
    ```

    * [Trino Documentation on Basic Authentication](https://trino.io/docs/current/security/password-file.html)
    * [Python Client Basic Authentication](https://github.com/trinodb/trino-python-client#basic-authentication)

=== "LDAP"

    | Option               | Description                                                             |  Type  | Required |
    |----------------------|-------------------------------------------------------------------------|:------:|:--------:|
    | `method`             | `ldap`                                                                  | string |    Y     |
    | `password`           | The password to use when authenticating.                                | string |    Y     |
    | `impersonation_user` | Override the provided username. This lets you impersonate another user. | string |    N     |

    ```yaml linenums="1"
    gateway_name:
      connection:
        type: trino
        method: ldap
        user: [user]
        password: [password]
        host: [host]
        catalog: [catalog]
    ```

    * [Trino Documentation on LDAP Authentication](https://trino.io/docs/current/security/ldap.html)
    * [Python Client LDAP Authentication](https://github.com/trinodb/trino-python-client#basic-authentication)

=== "Kerberos"

    | Option                           | Description                                                                       |  Type  | Required |
    |----------------------------------|-----------------------------------------------------------------------------------|:------:|:--------:|
    | `method`                         | `kerberos`                                                                        | string |    Y     |
    | `keytab`                         | Path to keytab. Ex: `/tmp/trino.keytab`                                           | string |    Y     |
    | `krb5_config`                    | Path to config. Ex: `/tmp/krb5.conf`                                              | string |    Y     |
    | `principal`                      | Principal.  Ex: `user@company.com`                                                | string |    Y     |
    | `service_name`                   | Service name (default is `trino`)                                                 | string |    N     |
    | `hostname_override`              | Kerberos hostname for a host whose DNS name doesn't match                         | string |    N     |
    | `mutual_authentication`          | Boolean flag for mutual authentication. Default: `false`                          |  bool  |    N     |
    | `force_preemptive`               | Boolean flag to preemptively initiate the Kerberos GSS exchange. Default: `false` |  bool  |    N     |
    | `sanitize_mutual_error_response` | Boolean flag to strip content and headers from error responses. Default: `true`   |  bool  |    N     |
    | `delegate`                       | Boolean flag for credential delegation (`GSS_C_DELEG_FLAG`). Default: `false`     |  bool  |    N     |

    ```yaml linenums="1"
    gateway_name:
      connection:
        type: trino
        method: kerberos
        user: user
        keytab: /tmp/trino.keytab
        krb5_config: /tmp/krb5.conf
        principal: trino@company.com
        host: trino.company.com
        catalog: datalake
    ```

    * [Trino Documentation on Kerberos Authentication](https://trino.io/docs/current/security/kerberos.html)
    * [Python Client Kerberos Authentication](https://github.com/trinodb/trino-python-client#kerberos-authentication)

=== "JWT"

    | Option      | Description     |  Type  | Required |
    |-------------|-----------------|:------:|:--------:|
    | `method`    | `jwt`           | string |    Y     |
    | `jwt_token` | The JWT string. | string |    Y     |

    ```yaml linenums="1"
    gateway_name:
      connection:
        type: trino
        method: jwt
        user: [user]
        password: [password]
        host: [host]
        catalog: [catalog]
    ```

    * [Trino Documentation on JWT Authentication](https://trino.io/docs/current/security/jwt.html)
    * [Python Client JWT Authentication](https://github.com/trinodb/trino-python-client#jwt-authentication)

=== "Certificate"

    | Option               | Description                                       |  Type  | Required |
    |----------------------|---------------------------------------------------|:------:|:--------:|
    | `method`             | `certificate`                                     | string |    Y     |
    | `cert`               | The full path to a certificate file               | string |    Y     |
    | `client_certificate` | Path to client certificate. Ex: `/tmp/client.crt` | string |    Y     |
    | `client_private_key` | Path to client private key. Ex: `/tmp/client.key` | string |    Y     |


    ```yaml linenums="1"
    gateway_name:
      connection:
        type: trino
        method: certificate
        user: [user]
        password: [password]
        host: [host]
        catalog: [catalog]
        cert: [path/to/cert_file]
        client_certificate: [path/to/client/cert]
        client_private_key: [path/to/client/key]
    ```

=== "Oauth"

    | Option               | Description                                       |  Type  | Required |
    |----------------------|---------------------------------------------------|:------:|:--------:|
    | `method`             | `oauth`                                           | string |    Y     |

    ```yaml linenums="1"
    gateway_name:
      connection:
        type: trino
        method: oauth
        host: trino.company.com
        catalog: datalake
    ```

    * [Trino Documentation on Oauth Authentication](https://trino.io/docs/current/security/oauth2.html)
    * [Python Client Oauth Authentication](https://github.com/trinodb/trino-python-client#oauth2-authentication)
