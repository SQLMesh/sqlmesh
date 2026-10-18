<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# Run the ClickHouse DataLake integration tests

This fixture runs ClickHouse's `DataLakeCatalog` against a local Iceberg REST catalog and MinIO. Run the commands from the repository root with a recent Docker Compose v2 release that supports `up --wait --wait-timeout`.

## Run the tests

Run the complete fixture and test module:

```bash
make clickhouse-datalake-test
```

If your running local Docker daemon is available through a named context, set `DOCKER_CONTEXT` for the command. For example, if the context is named `colima`:

```bash
DOCKER_CONTEXT=colima make clickhouse-datalake-test
```

Replace `colima` with the name of your running context. This setting applies only to this command and does not change Docker's global context. Use the same prefix with the `up` and `down` targets when needed.

The target checks the Docker daemon first. It then installs the repository's ClickHouse test environment and starts the dedicated Compose project.

Compose waits up to 120 seconds for these checks:

- MinIO can list `local/warehouse-rest` with `mc ls`.
- The REST catalog accepts a TCP connection on port `8181`.
- ClickHouse returns a result for `SELECT 1`.

Before MinIO creates the bucket, its init service retries `mc ready` up to 30 times. The target then seeds the Iceberg table and runs [the integration tests](../../test_integration_clickhouse_datalake.py) with `SQLMESH_RUN_CLICKHOUSE_DATALAKE=1`.

To start and seed the services without running pytest, use:

```bash
make clickhouse-datalake-up
```

To run pytest directly after startup, install the ClickHouse test dependencies and set the opt-in flag:

```bash
make engine-clickhouse-install
SQLMESH_RUN_CLICKHOUSE_DATALAKE=1 pytest -n 0 -m "docker and clickhouse_datalake" tests/core/engine_adapter/integration/test_integration_clickhouse_datalake.py
```

Running pytest without the flag skips the module. The test target fails at its `docker info` preflight if the daemon is unavailable. The startup target fails when Compose connects to an unavailable daemon. Either failure means the live tests did not run.

## Fixture behavior

The Compose project is named `sqlmesh-clickhouse-datalake`. It publishes ClickHouse HTTP on `127.0.0.1:18123`. The REST catalog and MinIO have no host ports. Test traffic uses the project network and fixture credentials. The bucket has no public policy. The REST catalog, seeder, and ClickHouse use only local fixture credentials. The upstream integration Compose stack makes its bucket public. This fixture leaves its bucket private. The tests do not read the shared integration profile or production credentials.

The Tabulario REST service uses `CATALOG_S3_PATH__STYLE__ACCESS=true` for path-style S3 access.

The [seed script](seed.py) connects to the REST catalog at `http://rest:8181`. PyIceberg adds the `/v1` path. ClickHouse connects to `http://rest:8181/v1`.

The script creates the `analytics.events` Iceberg table at `s3://warehouse-rest/data/sqlmesh/events`. It appends two rows, then one row, and checks that the table contains three rows across two snapshots. On rerun, it drops the table and clears objects under that table location before creating the table again.

The tests verify the ClickHouse server version and read the seeded table. They cover mapped reads with qualified tables, aliases, qualified columns, and CTEs, both with and without an injected virtual catalog.

Other cases cover AST immutability, raw SQL strings, CTAS, append and replace materializations, and rejection of an unknown source before target creation. They also check that local reads strip an injected virtual catalog. A mapped view succeeds without catalog injection. The same source fails when a virtual catalog is injected.

Run `make clickhouse-datalake-down` to stop the services. The target removes their containers, the project network, attached volumes, and orphan containers.

The next `make clickhouse-datalake-test` starts a clean project and reseeds the table.

## Pinned versions and source

The DataLakeCatalog setup and Iceberg seed flow follow ClickHouse's [`v25.8.16.34-lts` integration test](https://github.com/ClickHouse/ClickHouse/blob/v25.8.16.34-lts/tests/integration/test_database_iceberg/test.py) and [REST catalog Compose file](https://github.com/ClickHouse/ClickHouse/blob/v25.8.16.34-lts/tests/integration/compose/docker_compose_iceberg_rest_catalog.yml).

ClickHouse pins PyIceberg `0.8.1` and PyArrow `17.0.0` in its [integration runner requirements](https://github.com/ClickHouse/ClickHouse/blob/v25.8.16.34-lts/ci/docker/integration/runner/requirements.txt). This fixture also pins MinIO's Python client to `7.2.3` for cleanup.

The [fixture Compose file](../compose.clickhouse-datalake.yaml) pins these images by digest:

- `clickhouse/clickhouse-server:25.8.16.34` (`sha256:86e6acceeacb49202fd694b77b4f7fcd6e022b52a29b0568fbfb4b05e8f6b2e7`). This version matches the upstream test tag `v25.8.16.34-lts`.
- `tabulario/iceberg-rest:1.6.0` (`sha256:3b7d31bdfec626b68e97531c9778a1b9119659e456fe28545a49f6aa6a9ce472`).
- `cgr.dev/chainguard/minio` (`sha256:039800e64ec7247d2fde7cff3697e964f6fe20b6d7d2c46aa7d82cc63355d512`) and `cgr.dev/chainguard/minio-client:latest-dev` (`sha256:fb635b967f5f32424391150dac4985cc1aea78fb0a1ce031c01952cf661a13ec`).
- Seeder base image `python:3.12.13-slim-bookworm` (`sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`). Its package pins are in [requirements.txt](requirements.txt).

The MinIO images use Chainguard registry digests because the MinIO image references in the upstream Compose file were unavailable during version verification.

## Live validation status

The pinned fixture passed on a local ARM64 Docker daemon. The default Docker context was unavailable in this environment, so the commands used `DOCKER_CONTEXT=colima` to reach the running Colima daemon. This per-command setting did not change the global Docker context.

The seed script verified three rows across two Iceberg snapshots. The opt-in integration module passed all 19 cases with no skips, including after a clean `down` and `up` repeat. An unauthenticated request to the MinIO bucket endpoint returned HTTP `403 AccessDenied`, confirming that the bucket stayed private.
