from __future__ import annotations

import logging
import os
import sys
import typing as t
from pathlib import Path

import click

from sqlmesh import configure_logging, remove_excess_logs
from sqlmesh.cli import error_handler
from sqlmesh.cli import options as opt
from sqlmesh.cli.project_init import (
    InitCliMode,
    ProjectTemplate,
    init_example_project,
    interactive_init,
)
from sqlmesh.core.analytics import cli_analytics
from sqlmesh.core.config import load_configs
from sqlmesh.core.console import configure_console, get_console
from sqlmesh.core.context import Context
from sqlmesh.utils import Verbosity
from sqlmesh.utils.date import TimeLike
from sqlmesh.utils.errors import MissingDependencyError, SQLMeshError

logger = logging.getLogger(__name__)


SKIP_LOAD_COMMANDS = (
    "clean",
    "create_external_models",
    "destroy",
    "environments",
    "invalidate",
    "janitor",
    "migrate",
    "rollback",
    "run",
    "table_name",
)
SKIP_CONTEXT_COMMANDS = ("init", "ui")


def _sqlmesh_version() -> str:
    try:
        from sqlmesh import __version__

        return __version__
    except ImportError:
        return "0.0.0"


@click.group(no_args_is_help=True)
@click.version_option(version=_sqlmesh_version(), message="%(version)s")
@opt.paths
@opt.config
@click.option(
    "--gateway",
    type=str,
    help="The name of the gateway.",
    envvar="SQLMESH_GATEWAY",
)
@click.option(
    "--ignore-warnings",
    is_flag=True,
    help="Ignore warnings.",
    envvar="SQLMESH_IGNORE_WARNINGS",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug mode.",
)
@click.option(
    "--log-to-stdout",
    is_flag=True,
    help="Display logs in stdout.",
)
@click.option(
    "--log-file-dir",
    type=str,
    help="The directory to write log files to.",
)
@click.option(
    "--dotenv",
    type=click.Path(exists=True, path_type=Path),
    help="Path to a custom .env file to load environment variables.",
    envvar="SQLMESH_DOTENV_PATH",
)
@click.pass_context
@error_handler
def cli(
    ctx: click.Context,
    paths: t.List[str],
    config: t.Optional[str] = None,
    gateway: t.Optional[str] = None,
    ignore_warnings: bool = False,
    debug: bool = False,
    log_to_stdout: bool = False,
    log_file_dir: t.Optional[str] = None,
    dotenv: t.Optional[Path] = None,
) -> None:
    """SQLMesh command line tool."""
    if "--help" in sys.argv:
        return

    configure_logging(
        debug,
        log_to_stdout,
        log_file_dir=log_file_dir,
        ignore_warnings=ignore_warnings,
    )
    configure_console(ignore_warnings=ignore_warnings)

    load = True

    if len(paths) == 1:
        path = os.path.abspath(paths[0])
        if ctx.invoked_subcommand in SKIP_CONTEXT_COMMANDS:
            ctx.obj = path
            return
        if ctx.invoked_subcommand in SKIP_LOAD_COMMANDS:
            load = False

    configs = load_configs(config, Context.CONFIG_TYPE, paths, dotenv_path=dotenv)
    log_limit = list(configs.values())[0].log_limit

    remove_excess_logs(log_file_dir, log_limit)

    try:
        context = Context(
            paths=paths,
            config=configs,
            gateway=gateway,
            load=load,
        )
    except Exception:
        if debug:
            logger.exception("Failed to initialize SQLMesh context")
        raise

    if load and not context.models:
        raise click.ClickException(
            f"`{paths}` doesn't seem to have any models... cd into the proper directory or specify the path(s) with -p."
        )

    ctx.obj = context


@cli.command("init")
@click.argument("engine", required=False)
@click.option(
    "-t",
    "--template",
    type=str,
    help="Project template. Supported values: dbt, dlt, default, empty.",
)
@click.option(
    "--dlt-pipeline",
    type=str,
    help="DLT pipeline for which to generate a SQLMesh project. Use alongside template: dlt",
)
@click.option(
    "--dlt-path",
    type=str,
    help="The directory where the DLT pipeline resides. Use alongside template: dlt",
)
@click.pass_context
@error_handler
@cli_analytics
def init(
    ctx: click.Context,
    engine: t.Optional[str] = None,
    template: t.Optional[str] = None,
    dlt_pipeline: t.Optional[str] = None,
    dlt_path: t.Optional[str] = None,
) -> None:
    """Create a new SQLMesh repository."""
    project_template = None
    if template:
        try:
            project_template = ProjectTemplate(template.lower())
        except ValueError:
            template_strings = "', '".join([template.value for template in ProjectTemplate])
            raise click.ClickException(
                f"Invalid project template '{template}'. Please specify one of '{template_strings}'."
            )

    if engine or project_template == ProjectTemplate.DBT:
        init_example_project(
            path=ctx.obj,
            template=project_template or ProjectTemplate.DEFAULT,
            engine_type=engine,
            pipeline=dlt_pipeline,
            dlt_path=dlt_path,
        )
        return

    import sqlmesh.utils.rich as srich

    console = srich.console

    project_template, engine_type, cli_mode = interactive_init(ctx.obj, console, project_template)

    config_path = init_example_project(
        path=ctx.obj,
        template=project_template,
        engine_type=engine_type,
        cli_mode=cli_mode or InitCliMode.DEFAULT,
        pipeline=dlt_pipeline,
        dlt_path=dlt_path,
    )

    engine_install_text = ""
    if engine_type and engine_type not in ("duckdb", "motherduck"):
        install_text = (
            "pyspark" if engine_type == "spark" else f"sqlmesh\\[{engine_type.replace('_', '')}]"
        )
        engine_install_text = f'• Run command in CLI to install your SQL engine\'s Python dependencies: pip install "{install_text}"\n'
    # interactive init does not support DLT template
    next_step_text = {
        ProjectTemplate.DEFAULT: f"{engine_install_text}• Update your gateway connection settings (e.g., username/password) in the project configuration file:\n    {config_path}",
        ProjectTemplate.DBT: "",
    }
    next_step_text[ProjectTemplate.EMPTY] = next_step_text[ProjectTemplate.DEFAULT]

    quickstart_text = {
        ProjectTemplate.DEFAULT: "Quickstart guide:\nhttps://sqlmesh.readthedocs.io/en/stable/quickstart/cli/",
        ProjectTemplate.DBT: "dbt guide:\nhttps://sqlmesh.readthedocs.io/en/stable/integrations/dbt/",
    }
    quickstart_text[ProjectTemplate.EMPTY] = quickstart_text[ProjectTemplate.DEFAULT]

    console.print(f"""──────────────────────────────

Your SQLMesh project is ready!

Next steps:
{next_step_text[project_template]}
• Run command in CLI: sqlmesh plan
• (Optional) Explain a plan: sqlmesh plan --explain

{quickstart_text[project_template]}

Need help?
• Docs:   https://sqlmesh.readthedocs.io
• Slack:  https://www.tobikodata.com/slack
• GitHub: https://github.com/SQLMesh/sqlmesh/issues
""")


@cli.command("render")
@click.argument("model")
@opt.start_time
@opt.end_time
@opt.execution_time
@opt.expand
@click.option(
    "--dialect",
    type=str,
    help="The SQL dialect to render the query as.",
)
@click.option("--no-format", is_flag=True, help="Disable fancy formatting of the query.")
@opt.format_options
@click.pass_context
@error_handler
@cli_analytics
def render(
    ctx: click.Context,
    model: str,
    start: TimeLike,
    end: TimeLike,
    execution_time: t.Optional[TimeLike] = None,
    expand: t.Optional[t.Union[bool, t.Iterable[str]]] = None,
    dialect: t.Optional[str] = None,
    no_format: bool = False,
    **format_kwargs: t.Any,
) -> None:
    """Render a model's query, optionally expanding referenced models."""
    model = ctx.obj.get_model(model, raise_if_missing=True)

    rendered = ctx.obj.render(
        model,
        start=start,
        end=end,
        execution_time=execution_time,
        expand=expand,
    )

    format_config = ctx.obj.config_for_node(model).format
    format_kwargs = {
        **format_config.generator_options,
        **{k: v for k, v in format_kwargs.items() if v is not None},
    }

    sql = rendered.sql(
        pretty=True,
        dialect=ctx.obj.config.dialect if dialect is None else dialect,
        **format_kwargs,
    )
    if no_format:
        print(sql)
    else:
        ctx.obj.console.show_sql(sql)


@cli.command("evaluate")
@click.argument("model")
@opt.start_time
@opt.end_time
@opt.execution_time
@click.option(
    "--limit",
    type=int,
    help="The number of rows which the query should be limited to.",
)
@click.pass_context
@error_handler
@cli_analytics
def evaluate(
    ctx: click.Context,
    model: str,
    start: TimeLike,
    end: TimeLike,
    execution_time: t.Optional[TimeLike] = None,
    limit: t.Optional[int] = None,
) -> None:
    """Evaluate a model and return a dataframe with a default limit of 1000."""
    df = ctx.obj.evaluate(
        model,
        start=start,
        end=end,
        execution_time=execution_time,
        limit=limit,
    )
    if hasattr(df, "show"):
        df.show(limit)
    else:
        ctx.obj.console.log_success(df)


@cli.command("format")
@click.argument("paths", nargs=-1)
@click.option(
    "-t",
    "--transpile",
    type=str,
    help="Transpile project models to the specified dialect.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Whether or not to check formatting (but not actually format anything).",
    default=None,
)
@click.option(
    "--rewrite-casts/--no-rewrite-casts",
    is_flag=True,
    help="Rewrite casts to use the :: syntax.",
    default=None,
)
@click.option(
    "--append-newline",
    is_flag=True,
    help="Include a newline at the end of each file.",
    default=None,
)
@opt.format_options
@click.pass_context
@error_handler
@cli_analytics
def format(
    ctx: click.Context, paths: t.Optional[t.Tuple[str, ...]] = None, **kwargs: t.Any
) -> None:
    """Format all SQL models and audits."""
    if not ctx.obj.format(**{k: v for k, v in kwargs.items() if v is not None}, paths=paths):
        ctx.exit(1)


@cli.command("diff")
@click.argument("environment")
@click.pass_context
@error_handler
@cli_analytics
def diff(ctx: click.Context, environment: t.Optional[str] = None) -> None:
    """Show the diff between the local state and the target environment."""
    if ctx.obj.diff(environment, detailed=True):
        exit(1)


@cli.command("plan")
@click.argument("environment", required=False)
@opt.start_time
@opt.end_time
@opt.execution_time
@click.option(
    "--create-from",
    type=str,
    help="The environment to create the target environment from if it doesn't exist. Default: prod.",
)
@click.option(
    "--skip-tests",
    is_flag=True,
    help="Skip tests prior to generating the plan if they are defined.",
    default=None,
)
@click.option(
    "--skip-linter",
    is_flag=True,
    help="Skip linting prior to generating the plan if the linter is enabled.",
    default=None,
)
@click.option(
    "--restate-model",
    "-r",
    type=str,
    multiple=True,
    help="Restate data for specified models and models downstream from the one specified. For production environment, all related model versions will have their intervals wiped, but only the current versions will be backfilled. For development environment, only the current model versions will be affected.",
)
@click.option(
    "--no-gaps",
    is_flag=True,
    help="Ensure that new snapshots have no data gaps when comparing to existing snapshots for matching models in the target environment.",
    default=None,
)
@click.option(
    "--skip-backfill",
    "--dry-run",
    is_flag=True,
    help="Skip the backfill step and only create a virtual update for the plan.",
    default=None,
)
@click.option(
    "--empty-backfill",
    is_flag=True,
    help="Produce empty backfill. Like --skip-backfill no models will be backfilled, unlike --skip-backfill missing intervals will be recorded as if they were backfilled.",
    default=None,
)
@click.option(
    "--forward-only",
    is_flag=True,
    help="Create a plan for forward-only changes.",
    default=None,
)
@click.option(
    "--allow-destructive-model",
    type=str,
    multiple=True,
    help="Allow destructive forward-only changes to models whose names match the expression.",
)
@click.option(
    "--allow-additive-model",
    type=str,
    multiple=True,
    help="Allow additive forward-only changes to models whose names match the expression.",
)
@click.option(
    "--effective-from",
    type=str,
    required=False,
    help="The effective date from which to apply forward-only changes on production.",
)
@click.option(
    "--no-prompts",
    is_flag=True,
    help="Disable interactive prompts for the backfill time range. Please note that if this flag is set and there are uncategorized changes, plan creation will fail.",
    default=None,
)
@click.option(
    "--auto-apply",
    is_flag=True,
    help="Automatically apply the new plan after creation.",
    default=None,
)
@click.option(
    "--no-auto-categorization",
    is_flag=True,
    help="Disable automatic change categorization.",
    default=None,
)
@click.option(
    "--include-unmodified",
    is_flag=True,
    help="Include unmodified models in the target environment.",
    default=None,
)
@click.option(
    "--select-model",
    type=str,
    multiple=True,
    help="Select specific model changes that should be included in the plan.",
)
@click.option(
    "--backfill-model",
    type=str,
    multiple=True,
    help="Backfill only the models whose names match the expression.",
)
@click.option(
    "--no-diff",
    is_flag=True,
    help="Hide text differences for changed models.",
    default=None,
)
@click.option(
    "--run",
    is_flag=True,
    help="Run latest intervals as part of the plan application (prod environment only).",
    default=None,
)
@click.option(
    "--enable-preview",
    is_flag=True,
    help="Enable preview for forward-only models when targeting a development environment.",
    default=None,
)
@click.option(
    "--diff-rendered",
    is_flag=True,
    help="Output text differences for the rendered versions of the models and standalone audits.",
    default=None,
)
@click.option(
    "--explain",
    is_flag=True,
    help="Explain the plan instead of applying it.",
    default=None,
)
@click.option(
    "--ignore-cron",
    is_flag=True,
    help="Run all missing intervals, ignoring individual cron schedules. Only applies if --run is set.",
    default=None,
)
@click.option(
    "--min-intervals",
    default=None,
    help="For every model, ensure at least this many intervals are covered by a missing intervals check regardless of the plan start date",
)
@opt.verbose
@click.pass_context
@error_handler
@cli_analytics
def plan(
    ctx: click.Context,
    verbose: int,
    environment: t.Optional[str] = None,
    **kwargs: t.Any,
) -> None:
    """Apply local changes to the target environment."""
    context = ctx.obj
    restate_models = kwargs.pop("restate_model") or None
    select_models = kwargs.pop("select_model") or None
    allow_destructive_models = kwargs.pop("allow_destructive_model") or None
    allow_additive_models = kwargs.pop("allow_additive_model") or None
    backfill_models = kwargs.pop("backfill_model") or None
    ignore_cron = kwargs.pop("ignore_cron") or None
    setattr(get_console(), "verbosity", Verbosity(verbose))

    context.plan(
        environment,
        restate_models=restate_models,
        select_models=select_models,
        allow_destructive_models=allow_destructive_models,
        allow_additive_models=allow_additive_models,
        backfill_models=backfill_models,
        ignore_cron=ignore_cron,
        **kwargs,
    )


@cli.command("run")
@click.argument("environment", required=False)
@opt.start_time
@opt.end_time
@click.option("--skip-janitor", is_flag=True, help="Skip the janitor task.")
@click.option(
    "--ignore-cron",
    is_flag=True,
    help="Run for all missing intervals, ignoring individual cron schedules.",
)
@click.option(
    "--select-model",
    type=str,
    multiple=True,
    help="Select specific models to run. Note: this always includes upstream dependencies.",
)
@click.option(
    "--exit-on-env-update",
    type=int,
    help="If set, the command will exit with the specified code if the run is interrupted by an update to the target environment.",
)
@click.option(
    "--no-auto-upstream",
    is_flag=True,
    help="Do not automatically include upstream models. Only applicable when --select-model is used. Note: this may result in missing / invalid data for the selected models.",
)
@click.pass_context
@error_handler
@cli_analytics
def run(ctx: click.Context, environment: t.Optional[str] = None, **kwargs: t.Any) -> None:
    """Evaluate missing intervals for the target environment."""
    context = ctx.obj
    select_models = kwargs.pop("select_model") or None
    completion_status = context.run(environment, select_models=select_models, **kwargs)
    if completion_status.is_failure:
        raise click.ClickException("Run failed.")


@cli.command("invalidate")
@click.argument("environment", required=True)
@click.option(
    "--sync",
    "-s",
    is_flag=True,
    help="Wait for the environment to be deleted before returning. If not specified, the environment will be deleted asynchronously by the janitor process. This option requires a connection to the data warehouse.",
)
@click.pass_context
@error_handler
@cli_analytics
def invalidate(ctx: click.Context, environment: str, **kwargs: t.Any) -> None:
    """Invalidate the target environment, forcing its removal during the next run of the janitor process."""
    context = ctx.obj
    context.invalidate_environment(environment, **kwargs)


@cli.command("janitor")
@click.option(
    "--ignore-ttl",
    is_flag=True,
    help="Cleanup snapshots that are not referenced in any environment, regardless of when they're set to expire",
)
@click.option(
    "--force-delete",
    is_flag=True,
    help="Delete expired environment and snapshot state records even when the physical table or view drops fail. "
    "Any objects that could not be dropped become orphaned and must be removed manually.",
)
@click.pass_context
@error_handler
@cli_analytics
def janitor(ctx: click.Context, ignore_ttl: bool, force_delete: bool, **kwargs: t.Any) -> None:
    """
    Run the janitor process on-demand.

    The janitor cleans up old environments and expired snapshots.
    """
    ctx.obj.run_janitor(ignore_ttl, force_delete=force_delete, **kwargs)


@cli.command("destroy")
@click.pass_context
@error_handler
@cli_analytics
def destroy(ctx: click.Context, **kwargs: t.Any) -> None:
    """
    The destroy command removes all project resources.

    This includes engine-managed objects, state tables, the SQLMesh cache and any build artifacts.
    """
    ctx.obj.destroy(**kwargs)


@cli.command("dag")
@click.argument("file", required=True)
@click.option(
    "--select-model",
    type=str,
    multiple=True,
    help="Select specific models to include in the dag.",
)
@click.pass_context
@error_handler
@cli_analytics
def dag(ctx: click.Context, file: str, select_model: t.List[str]) -> None:
    """Render the DAG as an html file."""
    rendered_dag_path = ctx.obj.render_dag(file, select_model)
    if rendered_dag_path:
        ctx.obj.console.log_success(f"Generated the dag to {rendered_dag_path}")


@cli.command("create_test")
@click.argument("model")
@click.option(
    "-q",
    "--query",
    "queries",
    type=(str, str),
    multiple=True,
    default=[],
    help="Queries that will be used to generate data for the model's dependencies.",
)
@click.option(
    "-o",
    "--overwrite",
    "overwrite",
    is_flag=True,
    default=False,
    help="When true, the fixture file will be overwritten in case it already exists.",
)
@click.option(
    "-v",
    "--var",
    "variables",
    type=(str, str),
    multiple=True,
    help="Key-value pairs that will define variables needed by the model.",
)
@click.option(
    "-p",
    "--path",
    "path",
    help=(
        "The file path corresponding to the fixture, relative to the test directory. "
        "By default, the fixture will be created under the test directory and the file "
        "name will be inferred based on the test's name."
    ),
)
@click.option(
    "-n",
    "--name",
    "name",
    help="The name of the test that will be created. By default, it's inferred based on the model's name.",
)
@click.option(
    "--include-ctes",
    "include_ctes",
    is_flag=True,
    default=False,
    help="When true, CTE fixtures will also be generated.",
)
@click.pass_obj
@error_handler
@cli_analytics
def create_test(
    obj: Context,
    model: str,
    queries: t.List[t.Tuple[str, str]],
    overwrite: bool = False,
    variables: t.Optional[t.List[t.Tuple[str, str]]] = None,
    path: t.Optional[str] = None,
    name: t.Optional[str] = None,
    include_ctes: bool = False,
) -> None:
    """Generate a unit test fixture for a given model."""
    obj.create_test(
        model,
        input_queries=dict(queries),
        overwrite=overwrite,
        variables=dict(variables) if variables else None,
        path=path,
        name=name,
        include_ctes=include_ctes,
    )


@cli.command("test")
@opt.match_pattern
@opt.verbose
@click.option(
    "--preserve-fixtures",
    is_flag=True,
    default=False,
    help="Preserve the fixture tables in the testing database, useful for debugging.",
)
@click.argument("tests", nargs=-1)
@click.pass_obj
@error_handler
@cli_analytics
def test(
    obj: Context,
    k: t.List[str],
    verbose: int,
    preserve_fixtures: bool,
    tests: t.List[str],
) -> None:
    """Run model unit tests."""
    result = obj.test(
        match_patterns=k,
        tests=tests,
        verbosity=Verbosity(verbose),
        preserve_fixtures=preserve_fixtures,
    )
    if not result.wasSuccessful():
        exit(1)


@cli.command("audit")
@click.option(
    "--model",
    "models",
    multiple=True,
    help="A model to audit. Multiple models can be audited.",
)
@opt.start_time
@opt.end_time
@opt.execution_time
@click.pass_obj
@error_handler
@cli_analytics
def audit(
    obj: Context,
    models: t.Iterator[str],
    start: TimeLike,
    end: TimeLike,
    execution_time: t.Optional[TimeLike] = None,
) -> None:
    """Run audits for the target model(s)."""
    if not obj.audit(models=models, start=start, end=end, execution_time=execution_time):
        exit(1)


@cli.command("check_intervals")
@click.option(
    "--no-signals",
    is_flag=True,
    help="Disable signal checks and only show missing intervals.",
    default=False,
)
@click.argument("environment", required=False)
@click.option(
    "--select-model",
    type=str,
    multiple=True,
    help="Select specific models to show missing intervals for.",
)
@opt.start_time
@opt.end_time
@click.pass_context
@error_handler
@cli_analytics
def check_intervals(
    ctx: click.Context,
    environment: t.Optional[str],
    no_signals: bool,
    select_model: t.List[str],
    start: TimeLike,
    end: TimeLike,
) -> None:
    """Show missing intervals in an environment, respecting signals."""
    context = ctx.obj
    context.console.show_intervals(
        context.check_intervals(
            environment,
            no_signals=no_signals,
            select_models=select_model,
            start=start,
            end=end,
        )
    )


@cli.command("fetchdf")
@click.argument("sql")
@click.pass_context
@error_handler
@cli_analytics
def fetchdf(ctx: click.Context, sql: str) -> None:
    """Run a SQL query and display the results."""
    context = ctx.obj
    context.console.log_success(context.fetchdf(sql))


@cli.command("info")
@click.option(
    "--skip-connection",
    is_flag=True,
    help="Skip the connection test.",
)
@opt.verbose
@click.pass_obj
@error_handler
@cli_analytics
def info(obj: Context, skip_connection: bool, verbose: int) -> None:
    """
    Print information about a SQLMesh project.

    Includes counts of project models and macros and connection tests for the data warehouse.
    """
    obj.print_info(skip_connection=skip_connection, verbosity=Verbosity(verbose))


@cli.command("ui")
@click.option(
    "--host",
    type=str,
    default="127.0.0.1",
    help="Bind socket to this host. Default: 127.0.0.1",
)
@click.option(
    "--port",
    type=int,
    default=8000,
    help="Bind socket to this port. Default: 8000",
)
@click.option(
    "--mode",
    type=click.Choice(["ide", "catalog", "docs", "plan"], case_sensitive=False),
    default="ide",
    help="Mode to start the UI in. Default: ide",
)
@click.pass_context
@error_handler
@cli_analytics
def ui(ctx: click.Context, host: str, port: int, mode: str) -> None:
    """Start a browser-based SQLMesh UI."""
    from sqlmesh.core.console import get_console

    get_console().log_warning(
        "The UI is deprecated and will be removed in a future version. Please use the SQLMesh VSCode extension instead. "
        "Learn more at https://sqlmesh.readthedocs.io/en/stable/guides/vscode/"
    )

    try:
        import uvicorn
    except ModuleNotFoundError as e:
        raise MissingDependencyError(
            "Missing UI dependencies. Run `pip install 'sqlmesh[web]'` to install them."
        ) from e

    os.environ["PROJECT_PATH"] = ctx.obj
    os.environ["UI_MODE"] = mode
    if ctx.parent:
        config = ctx.parent.params.get("config")
        gateway = ctx.parent.params.get("gateway")
        if config:
            os.environ["CONFIG"] = config
        if gateway:
            os.environ["GATEWAY"] = gateway
    uvicorn.run(
        "web.server.app:app",
        host=host,
        port=port,
        log_level="info",
        timeout_keep_alive=300,
    )


@cli.command("migrate")
@click.pass_context
@error_handler
@cli_analytics
def migrate(ctx: click.Context) -> None:
    """Migrate SQLMesh to the current running version."""
    ctx.obj.migrate()


@cli.command("rollback")
@click.pass_obj
@error_handler
@cli_analytics
def rollback(obj: Context) -> None:
    """Rollback SQLMesh to the previous migration."""
    obj.rollback()


@cli.command("create_external_models")
@click.option(
    "--strict",
    is_flag=True,
    help="Raise an error if the external model is missing in the database",
)
@click.pass_obj
@error_handler
@cli_analytics
def create_external_models(obj: Context, **kwargs: t.Any) -> None:
    """Create a schema file containing external model schemas."""
    obj.create_external_models(**kwargs)


@cli.command("table_diff")
@click.argument("source_to_target", required=True, metavar="SOURCE:TARGET")
@click.argument("model", required=False)
@click.option(
    "-o",
    "--on",
    type=str,
    multiple=True,
    help="The column to join on. Can be specified multiple times. The model grain will be used if not specified.",
)
@click.option(
    "-s",
    "--skip-columns",
    type=str,
    multiple=True,
    help="The column(s) to skip when comparing the source and target table.",
)
@click.option(
    "--where",
    type=str,
    help="An optional where statement to filter results.",
)
@click.option(
    "--limit",
    type=int,
    default=20,
    help="The limit of the sample dataframe.",
)
@click.option(
    "--show-sample",
    is_flag=True,
    help="Show a sample of the rows that differ. With many columns, the output can be very wide.",
)
@click.option(
    "-d",
    "--decimals",
    type=int,
    default=3,
    help="The number of decimal places to keep when comparing floating point columns. Default: 3",
)
@click.option(
    "--skip-grain-check",
    is_flag=True,
    help="Disable the check for a primary key (grain) that is missing or is not unique.",
)
@click.option(
    "--warn-grain-check",
    is_flag=True,
    help="Warn if any selected model is missing a grain, and compute diffs for the remaining models.",
)
@click.option(
    "--temp-schema",
    type=str,
    help="Schema used for temporary tables. It can be `CATALOG.SCHEMA` or `SCHEMA`. Default: `sqlmesh_temp`",
)
@click.option(
    "--select-model",
    "-m",
    type=str,
    multiple=True,
    help="Specify one or more models to data diff. Use wildcards to diff multiple models. Ex: '*' (all models with applied plan diffs), 'demo.model+' (this and downstream models), 'git:feature_branch' (models with direct modifications in this branch only)",
)
@click.option(
    "--schema-diff-ignore-case",
    is_flag=True,
    help="If set, when performing a schema diff the case of column names is ignored when matching between the two schemas. For example, 'col_a' in the source schema and 'COL_A' in the target schema will be treated as the same column.",
)
@click.pass_obj
@error_handler
@cli_analytics
def table_diff(
    obj: Context, source_to_target: str, model: t.Optional[str], **kwargs: t.Any
) -> None:
    """Show the diff between two tables or a selection of models when they are specified."""
    source, target = source_to_target.split(":")
    select_model = kwargs.pop("select_model", None)

    if model and select_model:
        raise SQLMeshError(
            "The --select-model option cannot be used together with a model argument. Please choose one of them."
        )

    select_models = {model} if model else select_model
    obj.table_diff(
        source=source,
        target=target,
        select_models=select_models,
        **kwargs,
    )


@cli.command("rewrite")
@click.argument("sql")
@click.option(
    "--read",
    type=str,
    help="The input dialect of the sql string.",
)
@click.option(
    "--write",
    type=str,
    help="The output dialect of the sql string.",
)
@click.pass_obj
@error_handler
@cli_analytics
def rewrite(obj: Context, sql: str, read: str = "", write: str = "") -> None:
    """Rewrite a SQL expression with semantic references into an executable query.

    https://sqlmesh.readthedocs.io/en/latest/concepts/metrics/overview/
    """
    obj.console.show_sql(
        obj.rewrite(sql, dialect=read).sql(pretty=True, dialect=write or obj.config.dialect),
    )


@cli.command("clean")
@click.pass_obj
@error_handler
@cli_analytics
def clean(obj: Context) -> None:
    """Clears the SQLMesh cache and any build artifacts."""
    obj.clear_caches()


@cli.command("table_name")
@click.argument("model_name", required=True)
@click.option(
    "--environment",
    "--env",
    help="The environment to source the model version from.",
)
@click.option(
    "--prod",
    is_flag=True,
    default=False,
    help="If set, return the name of the physical table that will be used in production for the model version promoted in the target environment.",
)
@click.pass_obj
@error_handler
@cli_analytics
def table_name(
    obj: Context,
    model_name: str,
    environment: t.Optional[str] = None,
    prod: bool = False,
) -> None:
    """Prints the name of the physical table for the given model."""
    print(obj.table_name(model_name, environment, prod))


@cli.command("dlt_refresh")
@click.argument("pipeline", required=True)
@click.option(
    "-t",
    "--table",
    type=str,
    multiple=True,
    help="The specific dlt tables to refresh in the SQLMesh models.",
)
@click.option(
    "-f",
    "--force",
    is_flag=True,
    default=False,
    help="If set, existing models are overwritten with the new DLT tables.",
)
@click.option(
    "--dlt-path",
    type=str,
    help="The directory where the DLT pipeline resides.",
)
@click.pass_context
@error_handler
@cli_analytics
def dlt_refresh(
    ctx: click.Context,
    pipeline: str,
    force: bool,
    table: t.List[str] = [],
    dlt_path: t.Optional[str] = None,
) -> None:
    """Attaches to a DLT pipeline with the option to update specific or all missing tables in the SQLMesh project."""
    from sqlmesh.integrations.dlt import generate_dlt_models

    sqlmesh_models = generate_dlt_models(ctx.obj, pipeline, list(table or []), force, dlt_path)
    if sqlmesh_models:
        model_names = "\n".join([f"- {model_name}" for model_name in sqlmesh_models])
        ctx.obj.console.log_success(f"Updated SQLMesh project with models:\n{model_names}")
    else:
        ctx.obj.console.log_success("All SQLMesh models are up to date.")


@cli.command("environments")
@click.pass_obj
@error_handler
@cli_analytics
def environments(obj: Context) -> None:
    """Prints the list of SQLMesh environments with its expiry datetime."""
    obj.print_environment_names()


@cli.command("lint")
@click.option(
    "--models",
    "--model",
    multiple=True,
    help="A model to lint. Multiple models can be linted. If no models are specified, every model will be linted.",
)
@click.pass_obj
@error_handler
@cli_analytics
def lint(
    obj: Context,
    models: t.Iterator[str],
) -> None:
    """Run the linter for the target model(s)."""
    obj.lint_models(models)


@cli.group(no_args_is_help=True)
def state() -> None:
    """Commands for interacting with state"""
    pass


@state.command("export")
@click.option(
    "-o",
    "--output-file",
    required=True,
    help="Path to write the state export to",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
)
@click.option(
    "--environment",
    multiple=True,
    help="Name of environment to export. Specify multiple --environment arguments to export multiple environments",
)
@click.option(
    "--local",
    is_flag=True,
    help="Export local state only. Note that the resulting file will not be importable",
)
@click.option(
    "--no-confirm",
    is_flag=True,
    help="Do not prompt for confirmation before exporting existing state",
)
@click.pass_obj
@error_handler
@cli_analytics
def state_export(
    obj: Context,
    output_file: Path,
    environment: t.Optional[t.Tuple[str]],
    local: bool,
    no_confirm: bool,
) -> None:
    """Export the state database to a file"""
    confirm = not no_confirm

    if environment and local:
        raise click.ClickException("Cannot specify both --environment and --local")

    environment_names = list(environment) if environment else None
    obj.export_state(
        output_file=output_file,
        environment_names=environment_names,
        local_only=local,
        confirm=confirm,
    )


@state.command("import")
@click.option(
    "-i",
    "--input-file",
    help="Path to the state file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--replace",
    is_flag=True,
    help="Clear the remote state before loading the file. If omitted, a merge is performed instead",
)
@click.option(
    "--no-confirm",
    is_flag=True,
    help="Do not prompt for confirmation before updating existing state",
)
@click.pass_obj
@error_handler
@cli_analytics
def state_import(obj: Context, input_file: Path, replace: bool, no_confirm: bool) -> None:
    """Import a state export file back into the state database"""
    confirm = not no_confirm
    obj.import_state(input_file=input_file, clear=replace, confirm=confirm)
