# CLI

```
Usage: sqlmesh [OPTIONS] COMMAND [ARGS]...

  SQLMesh command line tool.

Options:
  --version            Show the version and exit.
  -p, --paths TEXT     Path(s) to the SQLMesh config/project.
  --config TEXT        Name of the config object. Only applicable to
                       configuration defined using Python script.
  --gateway TEXT       The name of the gateway.
  --ignore-warnings    Ignore warnings.
  --debug              Enable debug mode.
  --log-to-stdout      Display logs in stdout.
  --log-file-dir TEXT  The directory to write log files to.
  --dotenv PATH        Path to a custom .env file to load environment
                       variables. Can also be set via SQLMESH_DOTENV_PATH
                       environment variable.
  --help               Show this message and exit.

Commands:
  audit                   Run audits for the target model(s).
  clean                   Clears the SQLMesh cache and any build artifacts.
  create_external_models  Create a schema file containing external model...
  create_test             Generate a unit test fixture for a given model.
  dag                     Render the DAG as an html file.
  destroy                 The destroy command removes all project resources.
  diff                    Show the diff between the local state and the...
  dlt_refresh             Attaches to a DLT pipeline with the option to...
  environments            Prints the list of SQLMesh environments with...
  evaluate                Evaluate a model and return a dataframe with a...
  fetchdf                 Run a SQL query and display the results.
  format                  Format all SQL models and audits.
  info                    Print information about a SQLMesh project.
  init                    Create a new SQLMesh repository.
  invalidate              Invalidate the target environment, forcing its...
  janitor                 Run the janitor process on-demand.
  migrate                 Migrate SQLMesh to the current running version.
  plan                    Apply local changes to the target environment.
  prompt                  Uses LLM to generate a SQL query from a prompt.
  render                  Render a model's query, optionally expanding...
  rewrite                 Rewrite a SQL expression with semantic...
  rollback                Rollback SQLMesh to the previous migration.
  run                     Evaluate missing intervals for the target...
  state                   Commands for interacting with state
  table_diff              Show the diff between two tables.
  table_name              Prints the name of the physical table for the...
  test                    Run model unit tests.
  ui                      Start a browser-based SQLMesh UI.
  lint                    Run the linter for the target model(s).
```

## audit

```
Usage: sqlmesh audit [OPTIONS]

  Run audits for the target model(s).

Options:
  --model TEXT           A model to audit. Multiple models can be audited.
  -s, --start TEXT       The start datetime of the interval for which this
                         command will be applied.
  -e, --end TEXT         The end datetime of the interval for which this
                         command will be applied.
  --execution-time TEXT  The execution time (defaults to now).
  --help                 Show this message and exit.
```

## check_intervals

```
Usage: sqlmesh check_intervals [OPTIONS] [ENVIRONMENT]

  Show missing intervals in an environment, respecting signals.

Options:
  --no-signals         Disable signal checks and only show missing intervals.
  --select-model TEXT  Select specific models to show missing intervals for.
  -s, --start TEXT     The start datetime of the interval for which this
                       command will be applied.
  -e, --end TEXT       The end datetime of the interval for which this command
                       will be applied.
  --help               Show this message and exit.
```


## clean

```
Usage: sqlmesh clean [OPTIONS]

  Clears the SQLMesh cache and any build artifacts.

Options:
  --help  Show this message and exit.
```

## create_external_models

```
Usage: sqlmesh create_external_models [OPTIONS]

  Create a schema file containing external model schemas.

Options:
  --help  Show this message and exit.
```

## create_test

```
Usage: sqlmesh create_test [OPTIONS] MODEL

  Generate a unit test fixture for a given model.

Options:
  -q, --query <TEXT TEXT>...  Queries that will be used to generate data for
                              the model's dependencies.
  -o, --overwrite             When true, the fixture file will be overwritten
                              in case it already exists.
  -v, --var <TEXT TEXT>...    Key-value pairs that will define variables
                              needed by the model.
  -p, --path TEXT             The file path corresponding to the fixture,
                              relative to the test directory. By default, the
                              fixture will be created under the test directory
                              and the file name will be inferred based on the
                              test's name.
  -n, --name TEXT             The name of the test that will be created. By
                              default, it's inferred based on the model's
                              name.
  --include-ctes              When true, CTE fixtures will also be generated.
  --help                      Show this message and exit.
```

## dag

```
Usage: sqlmesh dag [OPTIONS] FILE

  Render the DAG as an html file.

Options:
  --select-model TEXT  Select specific models to include in the dag.
  --help               Show this message and exit.
```

## destroy

```
Usage: sqlmesh destroy

  Removes all state tables, the SQLMesh cache and all project resources, including warehouse objects. This includes all tables, views and schemas managed by SQLMesh, as well as any external resources that may have been created by other tools within those schemas.

Options:
  --help               Show this message and exit.
```

## dlt_refresh

```
Usage: dlt_refresh PIPELINE [OPTIONS]

  Attaches to a DLT pipeline with the option to update specific or all models of the SQLMesh project.

Options:
  -t, --table TEXT  The DLT tables to generate SQLMesh models from. When none specified, all new missing tables will be generated.
  -f, --force       If set it will overwrite existing models with the new generated models from the DLT tables.
  --help            Show this message and exit.
```

## diff

```
Usage: sqlmesh diff [OPTIONS] ENVIRONMENT

  Show the diff between the local state and the target environment.

Options:
  --help  Show this message and exit.
```

## environments
```
Usage: sqlmesh environments [OPTIONS]

  Prints the list of SQLMesh environments with its expiry datetime.

Options:
  --help             Show this message and exit.
```

## evaluate

```
Usage: sqlmesh evaluate [OPTIONS] MODEL

  Evaluate a model and return a dataframe with a default limit of 1000.

Options:
  -s, --start TEXT       The start datetime of the interval for which this
                         command will be applied.
  -e, --end TEXT         The end datetime of the interval for which this
                         command will be applied.
  --execution-time TEXT  The execution time (defaults to now).
  --limit INTEGER        The number of rows which the query should be limited
                         to.
  --help                 Show this message and exit.
```

## fetchdf

```
Usage: sqlmesh fetchdf [OPTIONS] SQL

  Run a SQL query and display the results.

Options:
  --help  Show this message and exit.
```

## format

```
Usage: sqlmesh format [OPTIONS]

  Format all SQL models and audits.

Options:
  -t, --transpile TEXT        Transpile project models to the specified
                              dialect.
  --append-newline            Include a newline at the end of each file.
  --no-rewrite-casts          Preserve the existing casts, without rewriting
                              them to use the :: syntax.
  --normalize                 Whether or not to normalize identifiers to
                              lowercase.
  --pad INTEGER               Determines the pad size in a formatted string.
  --indent INTEGER            Determines the indentation size in a formatted
                              string.
  --normalize-functions TEXT  Whether or not to normalize all function names.
                              Possible values are: 'upper', 'lower'
  --leading-comma             Determines whether or not the comma is leading
                              or trailing in select expressions. Default is
                              trailing.
  --max-text-width INTEGER    The max number of characters in a segment before
                              creating new lines in pretty mode.
  --check                     Whether or not to check formatting (but not
                              actually format anything).
  --help                      Show this message and exit.
```

## info

```
Usage: sqlmesh info [OPTIONS]

  Print information about a SQLMesh project.

  Includes counts of project models and macros and connection tests for the
  data warehouse.

Options:
  --skip-connection  Skip the connection test.
  -v, --verbose      Verbose output.
  --help  Show this message and exit.
```

## init

```
Usage: sqlmesh init [OPTIONS] [ENGINE]

  Create a new SQLMesh repository.

Options:
  -t, --template TEXT  Project template. Supported values: dbt, dlt, default,
                       empty.
  --dlt-pipeline TEXT  DLT pipeline for which to generate a SQLMesh project.
                       Use alongside template: dlt
  --dlt-path TEXT      The directory where the DLT pipeline resides. Use
                       alongside template: dlt
  --help               Show this message and exit.
```

## invalidate

```
Usage: sqlmesh invalidate [OPTIONS] ENVIRONMENT

  Invalidate the target environment, forcing its removal during the next run
  of the janitor process.

Options:
  -s, --sync  Wait for the environment to be deleted before returning. If not
              specified, the environment will be deleted asynchronously by the
              janitor process. This option requires a connection to the data
              warehouse.
  --help      Show this message and exit.
```

## janitor

```
Usage: sqlmesh janitor [OPTIONS]

  Run the janitor process on-demand.

  The janitor cleans up old environments and expired snapshots.

Options:
  --ignore-ttl  Cleanup snapshots that are not referenced in any environment,
                regardless of when they're set to expire
  --help        Show this message and exit.
```

## migrate

```
Usage: sqlmesh migrate [OPTIONS]

  Migrate SQLMesh to the current running version.

Options:
  --help  Show this message and exit.
```

!!! danger "Caution"

    The `migrate` command affects all SQLMesh users. Contact your SQLMesh administrator before running.

## plan

```
Usage: sqlmesh plan [OPTIONS] [ENVIRONMENT]

  Apply local changes to the target environment.

Options:
  -s, --start TEXT                The start datetime of the interval for which
                                  this command will be applied.
  -e, --end TEXT                  The end datetime of the interval for which
                                  this command will be applied.
  --execution-time TEXT           The execution time (defaults to now).
  --create-from TEXT              The environment to create the target
                                  environment from if it doesn't exist.
                                  Default: prod.
  --skip-tests                    Skip tests prior to generating the plan if
                                  they are defined.
  --skip-linter                   Skip linting prior to generating the plan if
                                  the linter is enabled.
  -r, --restate-model TEXT        Restate data for specified models and models
                                  downstream from the one specified. For
                                  production environment, all related model
                                  versions will have their intervals wiped,
                                  but only the current versions will be
                                  backfilled. For development environment,
                                  only the current model versions will be
                                  affected.
  --no-gaps                       Ensure that new snapshots have no data gaps
                                  when comparing to existing snapshots for
                                  matching models in the target environment.
  --skip-backfill, --dry-run      Skip the backfill step and only create a
                                  virtual update for the plan.
  --empty-backfill                Produce empty backfill. Like --skip-backfill
                                  no models will be backfilled, unlike --skip-
                                  backfill missing intervals will be recorded
                                  as if they were backfilled.
  --forward-only                  Create a plan for forward-only changes.
  --allow-destructive-model TEXT  Allow destructive forward-only changes to
                                  models whose names match the expression.
  --allow-additive-model TEXT     Allow additive forward-only changes to
                                  models whose names match the expression.
  --effective-from TEXT           The effective date from which to apply
                                  forward-only changes on production.
  --no-prompts                    Disable interactive prompts for the backfill
                                  time range. Please note that if this flag is
                                  set and there are uncategorized changes,
                                  plan creation will fail.
  --auto-apply                    Automatically apply the new plan after
                                  creation.
  --no-auto-categorization        Disable automatic change categorization.
  --include-unmodified            Include unmodified models in the target
                                  environment.
  --select-model TEXT             Select specific model changes that should be
                                  included in the plan.
  --backfill-model TEXT           Backfill only the models whose names match
                                  the expression.
  --no-diff                       Hide text differences for changed models.
  --run                           Run latest intervals as part of the plan
                                  application (prod environment only).
  --enable-preview                Enable preview for forward-only models when
                                  targeting a development environment.
  --diff-rendered                 Output text differences for the rendered
                                  versions of the models and standalone
                                  audits.
  --explain                       Explain the plan instead of applying it.
  -v, --verbose                   Verbose output. Use -vv for very verbose
                                  output.
  --help                          Show this message and exit.
```

## prompt

```
Usage: sqlmesh prompt [OPTIONS] PROMPT

  Uses LLM to generate a SQL query from a prompt.

Options:
  -e, --evaluate           Evaluate the generated SQL query and display the
                           results.
  -t, --temperature FLOAT  Sampling temperature. 0.0 - precise and
                           predictable, 0.5 - balanced, 1.0 - creative.
                           Default: 0.7
  -v, --verbose            Verbose output.
  --help                   Show this message and exit.
```

## render

```
Usage: sqlmesh render [OPTIONS] MODEL

  Render a model's query, optionally expanding referenced models.

Options:
  -s, --start TEXT            The start datetime of the interval for which
                              this command will be applied.
  -e, --end TEXT              The end datetime of the interval for which this
                              command will be applied.
  --execution-time TEXT       The execution time (defaults to now).
  --expand TEXT               Whether or not to expand materialized models
                              (defaults to False). If True, all referenced
                              models are expanded as raw queries. Multiple
                              model names can also be specified, in which case
                              only they will be expanded as raw queries.
  --dialect TEXT              The SQL dialect to render the query as.
  --no-format                 Disable fancy formatting of the query.
  --max-text-width INTEGER    The max number of characters in a segment before
                              creating new lines in pretty mode.
  --leading-comma             Determines whether or not the comma is leading
                              or trailing in select expressions. Default is
                              trailing.
  --normalize-functions TEXT  Whether or not to normalize all function names.
                              Possible values are: 'upper', 'lower'
  --indent INTEGER            Determines the indentation size in a formatted
                              string.
  --pad INTEGER               Determines the pad size in a formatted string.
  --normalize                 Whether or not to normalize identifiers to
                              lowercase.
  --help                      Show this message and exit.
```

## rewrite

```
Usage: sqlmesh rewrite [OPTIONS] SQL

  Rewrite a SQL expression with semantic references into an executable query.

  https://sqlmesh.readthedocs.io/en/latest/concepts/metrics/overview/

Options:
  --read TEXT   The input dialect of the sql string.
  --write TEXT  The output dialect of the sql string.
  --help        Show this message and exit.
```

## rollback

```
Usage: sqlmesh rollback [OPTIONS]

  Rollback SQLMesh to the previous migration.

Options:
  --help  Show this message and exit.
```

!!! danger "Caution"

    The `rollback` command affects all SQLMesh users. Contact your SQLMesh administrator before running.

## run

```
Usage: sqlmesh run [OPTIONS] [ENVIRONMENT]

  Evaluate missing intervals for the target environment.

Options:
  -s, --start TEXT              The start datetime of the interval for which
                                this command will be applied.
  -e, --end TEXT                The end datetime of the interval for which
                                this command will be applied.
  --skip-janitor                Skip the janitor task.
  --ignore-cron                 Run for all missing intervals, ignoring
                                individual cron schedules.
  --select-model TEXT           Select specific models to run. Note: this
                                always includes upstream dependencies.
  --exit-on-env-update INTEGER  If set, the command will exit with the
                                specified code if the run is interrupted by an
                                update to the target environment.
  --no-auto-upstream            Do not automatically include upstream models.
                                Only applicable when --select-model is used.
                                Note: this may result in missing / invalid
                                data for the selected models.
  --help                        Show this message and exit.
```

## state

```
Usage: sqlmesh state [OPTIONS] COMMAND [ARGS]...

  Commands for interacting with state

Options:
  --help  Show this message and exit.

Commands:
  export  Export the state database to a file
  import  Import a state export file back into the state database
```

### export

```
Usage: sqlmesh state export [OPTIONS]

  Export the state database to a file

Options:
  -o, --output-file FILE  Path to write the state export to  [required]
  --environment TEXT      Name of environment to export. Specify multiple
                          --environment arguments to export multiple
                          environments
  --local                 Export local state only. Note that the resulting
                          file will not be importable
  --no-confirm            Do not prompt for confirmation before exporting
                          existing state
  --help                  Show this message and exit.
```

### import

```
Usage: sqlmesh state import [OPTIONS]

  Import a state export file back into the state database

Options:
  -i, --input-file FILE  Path to the state file  [required]
  --replace              Clear the remote state before loading the file. If
                         omitted, a merge is performed instead
  --no-confirm           Do not prompt for confirmation before updating
                         existing state
  --help                 Show this message and exit.
```

## table_diff

```
Usage: sqlmesh table_diff [OPTIONS] SOURCE:TARGET [MODEL]

  Show the diff between two tables or multiple models across two environments.

Options:
  -o, --on TEXT            The column to join on. Can be specified multiple
                           times. The model grain will be used if not
                           specified.
  -s, --skip-columns TEXT  The column(s) to skip when comparing the source and
                           target table.
  --where TEXT             An optional where statement to filter results.
  --limit INTEGER          The limit of the sample dataframe.
  --show-sample            Show a sample of the rows that differ. With many
                           columns, the output can be very wide.
  -d, --decimals INTEGER   The number of decimal places to keep when comparing
                           floating point columns. Default: 3
  --skip-grain-check       Disable the check for a primary key (grain) that is
                           missing or is not unique.
  --warn-grain-check       Warn if any selected model is missing a grain,
                           and compute diffs for the remaining models.
  --temp-schema TEXT       Schema used for temporary tables. It can be
                           `CATALOG.SCHEMA` or `SCHEMA`. Default:
                           `sqlmesh_temp`
  -m, --select-model TEXT  Select specific models to table diff.
  --help                   Show this message and exit.
```

## table_name

```
Usage: sqlmesh table_name [OPTIONS] MODEL_NAME

  Prints the name of the physical table for the given model.

Options:
  --environment, --env TEXT  The environment to source the model version from.
  --prod                     If set, return the name of the physical table
                             that will be used in production for the model
                             version promoted in the target environment.
  --help                     Show this message and exit.
```

## test

```
Usage: sqlmesh test [OPTIONS] [TESTS]...

  Run model unit tests.

Options:
  -k TEXT              Only run tests that match the pattern of substring.
  -v, --verbose        Verbose output.
  --preserve-fixtures  Preserve the fixture tables in the testing database,
                       useful for debugging.
  --help               Show this message and exit.
```

## ui

```
Usage: sqlmesh ui [OPTIONS]

  Start a browser-based SQLMesh UI.

Options:
  --host TEXT                     Bind socket to this host. Default: 127.0.0.1
  --port INTEGER                  Bind socket to this port. Default: 8000
  --mode [ide|catalog|docs|plan]  Mode to start the UI in. Default: ide
  --help                          Show this message and exit.
```

## lint
```
Usage: sqlmesh lint [OPTIONS]
  Run linter for the target model(s).

Options:
  --model TEXT           A model to lint. Multiple models can be linted.  If no models are specified, every model will be linted.
  --help                 Show this message and exit.

```
