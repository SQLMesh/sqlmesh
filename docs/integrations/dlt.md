# dlt

SQLMesh enables efforless project generation using data ingested through [dlt](https://github.com/dlt-hub/dlt). This involves creating a baseline project scaffolding, generating incremental models to process the data from the pipeline's tables by inspecting its schema and configuring the gateway connection using the pipeline's credentials.

## Getting started
### Reading from a dlt pipeline

To load data from a dlt pipeline into SQLMesh, ensure the dlt pipeline has been run or restored locally. Then simply execute the sqlmesh `init` command *within the dlt project root directory* using the `dlt` template option and specifying the pipeline's name with the `dlt-pipeline` option:

```bash
sqlmesh init -t dlt --dlt-pipeline <pipeline-name> dialect
```

This will create the configuration file and directories, which are found in all SQLMesh projects:

- config.yaml
    - The file for project configuration. Refer to [configuration](../reference/configuration.md).
- ./models
    - SQL and Python models. Refer to [models](../concepts/models/overview.md).
- ./seeds
    - Seed files. Refer to [seeds](../concepts/models/seed_models.md).
- ./audits
    - Shared audit files. Refer to [auditing](../concepts/audits.md).
- ./tests
    - Unit test files. Refer to [testing](../concepts/tests.md).
- ./macros
    - Macro files. Refer to [macros](../concepts/macros/overview.md).

SQLMesh will also automatically generate models to ingest data from the pipeline incrementally. Incremental loading is ideal for large datasets where recomputing entire tables is resource-intensive. In this case utilizing the [`INCREMENTAL_BY_TIME_RANGE` model kind](../concepts/models/model_kinds.md#incremental_by_time_range). However, these model definitions can be customized to meet your specific project needs.

#### Specify the path to the pipelines directory

The default location for dlt pipelines is `~/.dlt/pipelines/<pipeline_name>`. If your pipelines are in a [different directory](https://dlthub.com/docs/general-usage/pipeline#separate-working-environments-with-pipelines_dir), use the `--dlt-path` argument to specify the path explicitly:

```bash
sqlmesh init -t dlt --dlt-pipeline <pipeline-name> --dlt-path <pipelines-directory> dialect
```

### Generating models on demand

To update the models in your SQLMesh project on demand, use the `dlt_refresh` command. This allows you to either specify individual tables to generate incremental models from or update all models at once.

- **Generate all missing tables**:

```bash
sqlmesh dlt_refresh <pipeline-name>
```

- **Generate all missing tables and overwrite existing ones** (use with `--force` or `-f`):

```bash
sqlmesh dlt_refresh <pipeline-name> --force
```

- **Generate specific dlt tables** (using `--table` or `-t`):

```bash
sqlmesh dlt_refresh <pipeline-name> --table <dlt-table>
```

- **Provide the explicit path to the pipelines directory** (using `--dlt-path`):

```bash
sqlmesh dlt_refresh <pipeline-name> --dlt-path <pipelines-directory>
```

#### Configuration

SQLMesh will retrieve the data warehouse connection credentials from your dlt project to configure the `config.yaml` file. This configuration can be modified or customized as needed. For more details, refer to the [configuration guide](../guides/configuration.md).

### Example

Generating a SQLMesh project dlt is quite simple. In this example, we'll use the example `sushi_pipeline.py` from the [sushi-dlt project](https://github.com/SQLMesh/sqlmesh/tree/main/examples/sushi_dlt).

First, run the pipeline within the project directory:

```bash
$ python sushi_pipeline.py
Pipeline sushi load step completed in 2.09 seconds
Load package 1728074157.660565 is LOADED and contains no failed jobs
```

After the pipeline has run, generate a SQLMesh project by executing:

```bash
sqlmesh init -t dlt --dlt-pipeline sushi duckdb
```

Then the SQLMesh project is all set up. You can then proceed to run the SQLMesh `plan` command to ingest the dlt pipeline data and populate the SQLMesh tables:

```bash
$ sqlmesh plan
`prod` environment will be initialized

Models:
└── Added:
    ├── sushi_dataset_sqlmesh.incremental__dlt_loads
    ├── sushi_dataset_sqlmesh.incremental_sushi_types
    └── sushi_dataset_sqlmesh.incremental_waiters
Models needing backfill (missing dates):
├── sushi_dataset_sqlmesh.incremental__dlt_loads: 2024-10-03 - 2024-10-03
├── sushi_dataset_sqlmesh.incremental_sushi_types: 2024-10-03 - 2024-10-03
└── sushi_dataset_sqlmesh.incremental_waiters: 2024-10-03 - 2024-10-03
Apply - Backfill Tables [y/n]: y
[1/1] sushi_dataset_sqlmesh.incremental__dlt_loads evaluated in 0.01s
[1/1] sushi_dataset_sqlmesh.incremental_sushi_types evaluated in 0.00s
[1/1] sushi_dataset_sqlmesh.incremental_waiters evaluated in 0.01s
Evaluating models ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 3/3 • 0:00:00


All model batches have been executed successfully

Virtually Updating 'prod' ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100.0% • 0:00:00

The target environment has been updated successfully
```

Once the models are planned and applied, you can continue as with any SQLMesh project, generating and applying [plans](../concepts/overview.md#make-a-plan), running [tests](../concepts/overview.md#tests) or [audits](../concepts/overview.md#audits), and executing models with a [scheduler](../guides/scheduling.md) if desired.
