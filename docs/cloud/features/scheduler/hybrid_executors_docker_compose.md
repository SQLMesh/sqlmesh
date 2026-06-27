# Tobiko Cloud Hybrid Executors - Docker Compose Setup

<div style="position: relative; padding-bottom: 56.25%; height: 0;"><iframe src="https://www.loom.com/embed/37815ec3ff4d41ea9b456fe234e13ec4?sid=94254851-c411-462b-ad90-8d20c201f822" frameborder="0" webkitallowfullscreen mozallowfullscreen allowfullscreen style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;"></iframe></div>

This Docker Compose configuration allows you to run Tobiko Cloud hybrid executors locally or on any server that supports Docker Compose.

Hybrid executors allow you to run operations on your own infrastructure while leveraging Tobiko Cloud for orchestration.

## What this setup provides

This setup deploys two hybrid executors that pass work tasks from Tobiko Cloud to your data warehouse in a secure way:

- **Apply Executor**: Handles applying changes to the data warehouse
- **Run Executor**: Handles scheduled model execution

Both executors must be properly configured with environment variables to connect to Tobiko Cloud and your data warehouse.

## Prerequisites

- Access to a [data warehouse supported by Tobiko Cloud](../../../integrations/overview.md#execution-engines) (e.g., Postgres, Snowflake, BigQuery)
- Docker and Docker Compose
- A Tobiko Cloud account with [client ID and client secret](../security/single_sign_on.md#provisioning-client-credentials)

## Quick start guide

1. **Get docker-compose file**:

   Download the [docker-compose.yml](https://raw.githubusercontent.com/SQLMesh/sqlmesh/refs/heads/main/docs/cloud/features/scheduler/scheduler/docker-compose.yml) and [.env.example](https://raw.githubusercontent.com/SQLMesh/sqlmesh/refs/heads/main/docs/cloud/features/scheduler/scheduler/.env.example) files to a local directory.

2. **Create your environment file**:

   Copy the downloaded example environment file into a new `.env` file:

   ```bash
   cp .env.example .env
   ```

3. **Edit the .env file** with your project's configuration:

   - Set your Tobiko Cloud organization, project, client ID, and client secret
   - Configure your gateway connection details
   - Adjust resource limits if needed

4. **Start the executors**:

   ```bash
   docker compose up -d
   ```

5. **Check the logs**:

   ```bash
   docker compose logs -f
   ```

## Configuration options

### Gateway configuration

The default configuration in the `docker-compose.yml` file uses Postgres, but you can use [any supported SQL engine](../../../integrations/overview.md#execution-engines) by adjusting the connection parameters in your `.env` file.

#### Multiple gateways

To configure multiple gateways, add additional environment variables for each gateway the `docker-compose.yml` file:

```yaml
environment:
  # First gateway
  SQLMESH__GATEWAYS__GATEWAY_A__CONNECTION__TYPE: ${DB_TYPE:-postgres}
  # ... other GATEWAY_A configuration ...

  # Second gateway
  SQLMESH__GATEWAYS__GATEWAY_B__CONNECTION__TYPE: snowflake
  SQLMESH__GATEWAYS__GATEWAY_B__CONNECTION__ACCOUNT: ${SNOWFLAKE_ACCOUNT}
  # ... other GATEWAY_B configuration ...
```

## Health checking

Verify the health of your executors by running these commands:

```bash
docker compose exec apply-executor /app/pex executor apply --check
docker compose exec run-executor /app/pex executor run --check
```

Example successful output:

```bash
> docker compose exec apply-executor /app/pex executor apply --check
2025-04-09 21:24:49,873 - MainThread - httpx - INFO - HTTP Request: GET https://cloud.tobikodata.com/sqlmesh/<YOUR ORG>/<YOUR PROJECT>/api/state-sync/enterprise-version/upgrade "HTTP/1.1 200 OK" (_client.py:1025)
2025-04-09 21:24:49,889 - MainThread - tobikodata.tcloud.installer - INFO - Executor is installed (installer.py:180)
```

In addition, ensure the executors are healthy by running `echo $?` to confirm the check command returned exit code 0.

## Stopping the executors

To stop the executors:

```bash
docker compose down
```

## Troubleshooting

If you encounter issues:

1. Check the logs: `docker compose logs -f`
2. Verify your connection settings in the `.env` file
3. Ensure your client ID and client secret are correct
4. Check that your SQL engine is accessible from the Docker containers

## Security considerations

!!! warning "Never commit .env to version control"

   The `.env` file contains sensitive information. Never commit it to version control.

- Consider using Docker secrets or a secrets management solution in production environments.
- For production deployments, consider using the Kubernetes Helm chart instead, which offers more robust reliability and secret management options.