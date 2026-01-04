from __future__ import annotations

from io import StringIO

import functools
import logging
import typing as t
from argparse import Namespace, SUPPRESS
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from hyperscript import h

try:
    from IPython.core.display import display  # type: ignore
except ImportError:
    from IPython.display import display

from IPython.core.magic import (
    Magics,
    cell_magic,
    line_cell_magic,
    line_magic,
    magics_class,
)
from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring
from IPython.utils.process import arg_split
from rich.jupyter import JupyterRenderable
from sqlmesh.cli.project_init import ProjectTemplate, init_example_project
from sqlmesh.core import analytics
from sqlmesh.core.config import load_configs
from sqlmesh.core.config.connection import INIT_DISPLAY_INFO_TO_TYPE
from sqlmesh.core.console import create_console, set_console, configure_console
from sqlmesh.core.context import Context
from sqlmesh.core.dialect import format_model_expressions, parse
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.test import ModelTestMetadata
from sqlmesh.utils import yaml, Verbosity, optional_import
from sqlmesh.utils.errors import MagicError, MissingContextException, SQLMeshError

logger = logging.getLogger(__name__)

CONTEXT_VARIABLE_NAMES = [
    "context",
    "ctx",
    "sqlmesh",
]


def pass_sqlmesh_context(func: t.Callable) -> t.Callable:
    @functools.wraps(func)
    def wrapper(self: SQLMeshMagics, *args: t.Any, **kwargs: t.Any) -> None:
        for variable_name in CONTEXT_VARIABLE_NAMES:
            context = self._shell.user_ns.get(variable_name)
            if isinstance(context, Context):
                break
        else:
            raise MissingContextException(
                f"Context must be defined and initialized with one of these names: {', '.join(CONTEXT_VARIABLE_NAMES)}"
            )
        old_console = context.console
        new_console = create_console(display=self.display)
        context.console = new_console
        set_console(new_console)
        context.refresh()

        magic_name = func.__name__
        bound_method = getattr(self, magic_name, None)
        if bound_method:
            args_split = arg_split(args[0])
            parser = bound_method.parser

            original_parser_actions = deepcopy(parser._actions)
            original_parser_defaults = parser._defaults

            # Temporarily supress default values, otherwise any missing arg would be set and affect analytics
            parser._defaults = {}
            for action in parser._actions:
                action.default = SUPPRESS

            parsed_args, _ = parser.parse_known_args(args_split, Namespace())

            parser._actions = original_parser_actions
            parser._defaults = original_parser_defaults

            command_args = {k for k, v in parsed_args.__dict__.items() if v is not None}
            analytics.collector.on_magic_command(command_name=magic_name, command_args=command_args)

        func(self, context, *args, **kwargs)

        context.console = old_console
        set_console(old_console)

    return wrapper


def parse_expand(value: str) -> t.Union[bool, t.List[str]]:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return [name.strip() for name in value.split(",") if name.strip()]


def format_arguments(func: t.Callable) -> t.Callable:
    """Decorator to add common format arguments to magic commands."""
    func = argument(
        "--normalize",
        action="store_true",
        help="Whether or not to normalize identifiers to lowercase.",
        default=None,
    )(func)
    func = argument(
        "--pad",
        type=int,
        help="Determines the pad size in a formatted string.",
    )(func)
    func = argument(
        "--indent",
        type=int,
        help="Determines the indentation size in a formatted string.",
    )(func)
    func = argument(
        "--normalize-functions",
        type=str,
        help="Whether or not to normalize all function names. Possible values are: 'upper', 'lower'",
    )(func)
    func = argument(
        "--leading-comma",
        action="store_true",
        help="Determines whether or not the comma is leading or trailing in select expressions. Default is trailing.",
        default=None,
    )(func)
    func = argument(
        "--max-text-width",
        type=int,
        help="The max number of characters in a segment before creating new lines in pretty mode.",
    )(func)
    return func


@magics_class
class SQLMeshMagics(Magics):
    @property
    def display(self) -> t.Callable:
        from sqlmesh import RuntimeEnv

        if RuntimeEnv.get().is_databricks:
            # Use Databricks' special display instead of the normal IPython display
            return self._shell.user_ns["display"]
        return display

    @property
    def _shell(self) -> t.Any:
        # Make mypy happy.
        if not self.shell:
            raise RuntimeError("IPython Magics are in invalid state")
        return self.shell

    @magic_arguments()
    @argument(
        "paths",
        type=str,
        nargs="+",
        default="",
        help="The path(s) to the SQLMesh project(s).",
    )
    @argument(
        "--config",
        type=str,
        help="Name of the config object. Only applicable to configuration defined using Python script.",
    )
    @argument("--gateway", type=str, help="The name of the gateway.")
    @argument("--ignore-warnings", action="store_true", help="Ignore warnings.")
    @argument("--debug", action="store_true", help="Enable debug mode.")
    @argument("--log-file-dir", type=str, help="The directory to write the log file to.")
    @argument(
        "--dotenv", type=str, help="Path to a custom .env file to load environment variables from."
    )
    @line_magic
    def context(self, line: str) -> None:
        """Sets the context in the user namespace."""
        from sqlmesh import configure_logging, remove_excess_logs

        args = parse_argstring(self.context, line)
        log_file_dir = args.log_file_dir

        configure_logging(
            args.debug,
            log_file_dir=log_file_dir,
            ignore_warnings=args.ignore_warnings,
        )
        configure_console(ignore_warnings=args.ignore_warnings)

        dotenv_path = Path(args.dotenv) if args.dotenv else None
        configs = load_configs(
            args.config, Context.CONFIG_TYPE, args.paths, dotenv_path=dotenv_path
        )
        log_limit = list(configs.values())[0].log_limit

        remove_excess_logs(log_file_dir, log_limit)

        try:
            context = Context(paths=args.paths, config=configs, gateway=args.gateway)
            self._shell.user_ns["context"] = context
        except Exception:
            if args.debug:
                logger.exception("Failed to initialize SQLMesh context")
            raise

        context.console.log_success(f"SQLMesh project context set to: {', '.join(args.paths)}")

    @magic_arguments()
    @argument("path", type=str, help="The path where the new SQLMesh project should be created.")
    @argument(
        "engine",
        type=str,
        help=f"Project SQL engine. Supported values: '{', '.join([info[1] for info in sorted(INIT_DISPLAY_INFO_TO_TYPE.values(), key=lambda x: x[0])])}'.",  # type: ignore
    )
    @argument(
        "--template",
        "-t",
        type=str,
        help="Project template. Supported values: dbt, default, empty.",
    )
    @argument(
        "--dlt-pipeline",
        type=str,
        help="DLT pipeline for which to generate a SQLMesh project. Use alongside template: dlt",
    )
    @argument(
        "--dlt-path",
        type=str,
        help="The directory where the DLT pipeline resides. Use alongside template: dlt",
    )
    @line_magic
    def init(self, line: str) -> None:
        """Creates a SQLMesh project scaffold with a default SQL dialect."""
        args = parse_argstring(self.init, line)
        try:
            project_template = ProjectTemplate(
                args.template.lower() if args.template else "default"
            )
        except ValueError:
            raise MagicError(f"Invalid project template '{args.template}'")
        init_example_project(
            path=args.path,
            engine_type=args.engine,
            dialect=None,
            template=project_template,
            pipeline=args.dlt_pipeline,
            dlt_path=args.dlt_path,
        )
        html = str(
            h(
                "div",
                h(
                    "span",
                    {"style": {"color": "green", "font-weight": "bold"}},
                    "SQLMesh project scaffold created",
                ),
            )
        )
        self.display(JupyterRenderable(html=html, text=""))

    @magic_arguments()
    @argument("model", type=str, help="The model.")
    @argument("--start", "-s", type=str, help="Start date to render.")
    @argument("--end", "-e", type=str, help="End date to render.")
    @argument("--execution-time", type=str, help="Execution time.")
    @argument("--dialect", "-d", type=str, help="The rendered dialect.")
    @line_cell_magic
    @pass_sqlmesh_context
    def model(self, context: Context, line: str, sql: t.Optional[str] = None) -> None:
        """Renders the model and automatically fills in an editable cell with the model definition."""
        args = parse_argstring(self.model, line)

        model = context.get_model(args.model, raise_if_missing=True)
        config = context.config_for_node(model)

        if sql:
            expressions = parse(sql, default_dialect=config.dialect)
            loaded = load_sql_based_model(
                expressions,
                macros=context._macros,
                jinja_macros=context._jinja_macros,
                path=model._path,
                dialect=config.dialect,
                time_column_format=config.time_column_format,
                physical_schema_mapping=context.config.physical_schema_mapping,
                default_catalog=context.default_catalog,
            )

            if loaded.name == args.model:
                model = loaded
        else:
            if model._path:
                with open(model._path, "r", encoding="utf-8") as file:
                    expressions = parse(file.read(), default_dialect=config.dialect)

        formatted = format_model_expressions(
            expressions,
            model.dialect,
            rewrite_casts=not config.format.no_rewrite_casts,
            **config.format.generator_options,
        )

        self._shell.set_next_input(
            "\n".join(
                [
                    " ".join(["%%model", line]),
                    formatted,
                ]
            ),
            replace=True,
        )

        if model._path:
            with open(model._path, "w", encoding="utf-8") as file:
                file.write(formatted)

        if sql:
            context.console.log_success(f"Model `{args.model}` updated")

        context.upsert_model(model)
        context.console.show_sql(
            context.render(
                model.name,
                start=args.start,
                end=args.end,
                execution_time=args.execution_time,
            ).sql(pretty=True, dialect=args.dialect or model.dialect)
        )

    @magic_arguments()
    @argument("model", type=str, help="The model.")
    @argument("test_name", type=str, nargs="?", default=None, help="The test name to display")
    @argument("--ls", action="store_true", help="List tests associated with a model")
    @line_cell_magic
    @pass_sqlmesh_context
    def test(self, context: Context, line: str, test_def_raw: t.Optional[str] = None) -> None:
        """Allow the user to list tests for a model, output a specific test, and then write their changes back"""
        args = parse_argstring(self.test, line)
        if not args.test_name and not args.ls:
            raise MagicError("Must provide either test name or `--ls` to list tests")

        test_meta = context.select_tests()

        tests: t.Dict[str, t.Dict[str, ModelTestMetadata]] = defaultdict(dict)
        for model_test_metadata in test_meta:
            model = model_test_metadata.body.get("model")
            if not model:
                context.console.log_error(
                    f"Test found that does not have `model` defined: {model_test_metadata.path}"
                )
            else:
                tests[model][model_test_metadata.test_name] = model_test_metadata

        model = context.get_model(args.model, raise_if_missing=True)

        if args.ls:
            # TODO: Provide better UI for displaying tests
            for test_name in tests[model.name]:
                context.console.log_status_update(test_name)
            return

        test = tests[model.name][args.test_name]
        test_def = yaml.load(test_def_raw) if test_def_raw else test.body
        test_def_output = yaml.dump(test_def)

        self._shell.set_next_input(
            "\n".join(
                [
                    " ".join(["%%test", line]),
                    test_def_output,
                ]
            ),
            replace=True,
        )

        with open(test.path, "r+", encoding="utf-8") as file:
            content = yaml.load(file.read())
            content[args.test_name] = test_def
            file.seek(0)
            yaml.dump(content, file)
            file.truncate()

    @magic_arguments()
    @argument(
        "environment",
        nargs="?",
        type=str,
        help="The environment to run the plan against",
    )
    @argument("--start", "-s", type=str, help="Start date to backfill.")
    @argument("--end", "-e", type=str, help="End date to backfill.")
    @argument("--execution-time", type=str, help="Execution time.")
    @argument(
        "--create-from",
        type=str,
        help="The environment to create the target environment from if it doesn't exist. Default: prod.",
    )
    @argument(
        "--skip-tests",
        "-t",
        action="store_true",
        help="Skip the unit tests defined for the model.",
    )
    @argument(
        "--skip-linter",
        action="store_true",
        help="Skip the linter for the model.",
    )
    @argument(
        "--restate-model",
        "-r",
        type=str,
        nargs="*",
        help="Restate data for specified models (and models downstream from the one specified). For production environment, all related model versions will have their intervals wiped, but only the current versions will be backfilled. For development environment, only the current model versions will be affected.",
    )
    @argument(
        "--no-gaps",
        "-g",
        action="store_true",
        help="Ensure that new snapshots have no data gaps when comparing to existing snapshots for matching models in the target environment.",
    )
    @argument(
        "--skip-backfill",
        "--dry-run",
        action="store_true",
        help="Skip the backfill step and only create a virtual update for the plan.",
    )
    @argument(
        "--empty-backfill",
        action="store_true",
        help="Produce empty backfill. Like --skip-backfill no models will be backfilled, unlike --skip-backfill missing intervals will be recorded as if they were backfilled.",
    )
    @argument(
        "--forward-only",
        action="store_true",
        help="Create a plan for forward-only changes.",
        default=None,
    )
    @argument(
        "--effective-from",
        type=str,
        help="The effective date from which to apply forward-only changes on production.",
    )
    @argument(
        "--no-prompts",
        action="store_true",
        help="Disables interactive prompts for the backfill time range. Please note that if this flag is set and there are uncategorized changes, plan creation will fail.",
        default=None,
    )
    @argument(
        "--auto-apply",
        action="store_true",
        help="Automatically applies the new plan after creation.",
        default=None,
    )
    @argument(
        "--no-auto-categorization",
        action="store_true",
        help="Disable automatic change categorization.",
        default=None,
    )
    @argument(
        "--include-unmodified",
        action="store_true",
        help="Include unmodified models in the target environment.",
        default=None,
    )
    @argument(
        "--select-model",
        type=str,
        nargs="*",
        help="Select specific model changes that should be included in the plan.",
    )
    @argument(
        "--backfill-model",
        type=str,
        nargs="*",
        help="Backfill only the models whose names match the expression.",
    )
    @argument(
        "--no-diff",
        action="store_true",
        help="Hide text differences for changed models.",
        default=None,
    )
    @argument(
        "--run",
        action="store_true",
        help="Run latest intervals as part of the plan application (prod environment only).",
    )
    @argument(
        "--ignore-cron",
        action="store_true",
        help="Run for all missing intervals, ignoring individual cron schedules. Only applies if --run is set.",
        default=None,
    )
    @argument(
        "--enable-preview",
        action="store_true",
        help="Enable preview for forward-only models when targeting a development environment.",
        default=None,
    )
    @argument(
        "--diff-rendered",
        action="store_true",
        help="Output text differences for the rendered versions of the models and standalone audits",
    )
    @argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Verbose output. Use -vv for very verbose.",
    )
    @line_magic
    @pass_sqlmesh_context
    def plan(self, context: Context, line: str) -> None:
        """Goes through a set of prompts to both establish a plan and apply it"""
        args = parse_argstring(self.plan, line)

        setattr(context.console, "verbosity", Verbosity(args.verbose))

        context.plan(
            args.environment,
            start=args.start,
            end=args.end,
            execution_time=args.execution_time,
            create_from=args.create_from,
            skip_tests=args.skip_tests,
            restate_models=args.restate_model,
            backfill_models=args.backfill_model,
            no_gaps=args.no_gaps,
            skip_backfill=args.skip_backfill,
            empty_backfill=args.empty_backfill,
            forward_only=args.forward_only,
            no_prompts=args.no_prompts,
            auto_apply=args.auto_apply,
            no_auto_categorization=args.no_auto_categorization,
            effective_from=args.effective_from,
            include_unmodified=args.include_unmodified,
            select_models=args.select_model,
            no_diff=args.no_diff,
            run=args.run,
            ignore_cron=args.run,
            enable_preview=args.enable_preview,
            diff_rendered=args.diff_rendered,
        )

    @magic_arguments()
    @argument(
        "environment",
        nargs="?",
        type=str,
        help="The environment to run against",
    )
    @argument("--start", "-s", type=str, help="Start date to evaluate.")
    @argument("--end", "-e", type=str, help="End date to evaluate.")
    @argument("--skip-janitor", action="store_true", help="Skip the janitor task.")
    @argument(
        "--ignore-cron",
        action="store_true",
        help="Run for all missing intervals, ignoring individual cron schedules.",
    )
    @argument(
        "--select-model",
        type=str,
        nargs="*",
        help="Select specific models to run. Note: this always includes upstream dependencies.",
    )
    @argument(
        "--exit-on-env-update",
        type=int,
        help="If set, the command will exit with the specified code if the run is interrupted by an update to the target environment.",
    )
    @argument(
        "--no-auto-upstream",
        action="store_true",
        help="Do not automatically include upstream models. Only applicable when --select-model is used. Note: this may result in missing / invalid data for the selected models.",
    )
    @line_magic
    @pass_sqlmesh_context
    def run_dag(self, context: Context, line: str) -> None:
        """Evaluate the DAG of models using the built-in scheduler."""
        args = parse_argstring(self.run_dag, line)

        completion_status = context.run(
            args.environment,
            start=args.start,
            end=args.end,
            skip_janitor=args.skip_janitor,
            ignore_cron=args.ignore_cron,
            select_models=args.select_model,
            exit_on_env_update=args.exit_on_env_update,
            no_auto_upstream=args.no_auto_upstream,
        )
        if completion_status.is_failure:
            raise SQLMeshError("Error Running DAG. Check logs for details.")

    @magic_arguments()
    @argument("model", type=str, help="The model.")
    @argument("--start", "-s", type=str, help="Start date to render.")
    @argument("--end", "-e", type=str, help="End date to render.")
    @argument("--execution-time", type=str, help="Execution time.")
    @argument(
        "--limit",
        type=int,
        help="The number of rows which the query should be limited to.",
    )
    @line_magic
    @pass_sqlmesh_context
    def evaluate(self, context: Context, line: str) -> None:
        """Evaluate a model query and fetches a dataframe."""
        context.refresh()

        snowpark = optional_import("snowflake.snowpark")
        args = parse_argstring(self.evaluate, line)

        df = context.evaluate(
            args.model,
            start=args.start,
            end=args.end,
            execution_time=args.execution_time,
            limit=args.limit,
        )

        if snowpark and isinstance(df, snowpark.DataFrame):
            df = df.limit(args.limit or 100).to_pandas()

        self.display(df)

    @magic_arguments()
    @argument("model", type=str, help="The model.")
    @argument("--start", "-s", type=str, help="Start date to render.")
    @argument("--end", "-e", type=str, help="End date to render.")
    @argument("--execution-time", type=str, help="Execution time.")
    @argument(
        "--expand",
        type=parse_expand,
        help="Whether or not to use expand materialized models, defaults to False. If 'true', all referenced models are expanded as raw queries. If a comma-separated list of model names, only those models are expanded as raw queries.",
    )
    @argument("--dialect", type=str, help="SQL dialect to render.")
    @argument("--no-format", action="store_true", help="Disable fancy formatting of the query.")
    @format_arguments
    @line_magic
    @pass_sqlmesh_context
    def render(self, context: Context, line: str) -> None:
        """Renders a model's query, optionally expanding referenced models."""
        context.refresh()
        render_opts = vars(parse_argstring(self.render, line))
        model = render_opts.pop("model")
        dialect = render_opts.pop("dialect", None)
        expand = render_opts.pop("expand", False)

        model = context.get_model(model, raise_if_missing=True)

        query = context.render(
            model,
            start=render_opts.pop("start", None),
            end=render_opts.pop("end", None),
            execution_time=render_opts.pop("execution_time", None),
            expand=expand,
        )

        no_format = render_opts.pop("no_format", False)

        format_config = context.config_for_node(model).format
        format_options = {
            **format_config.generator_options,
            **{k: v for k, v in render_opts.items() if v is not None},
        }

        sql = query.sql(
            pretty=True,
            dialect=context.config.dialect if dialect is None else dialect,
            **format_options,
        )

        if no_format:
            context.console.log_status_update(sql)
        else:
            context.console.show_sql(sql)

    @magic_arguments()
    @argument(
        "df_var",
        default=None,
        nargs="?",
        type=str,
        help="An optional variable name to store the resulting dataframe.",
    )
    @cell_magic
    @pass_sqlmesh_context
    def fetchdf(self, context: Context, line: str, sql: str) -> None:
        """Fetches a dataframe from sql, optionally storing it in a variable."""
        args = parse_argstring(self.fetchdf, line)
        df = context.fetchdf(sql)
        if args.df_var:
            self._shell.user_ns[args.df_var] = df
        self.display(df)

    @magic_arguments()
    @argument("--file", "-f", type=str, help="An optional file path to write the HTML output to.")
    @argument(
        "--select-model",
        type=str,
        nargs="*",
        help="Select specific models to include in the dag.",
    )
    @line_magic
    @pass_sqlmesh_context
    def dag(self, context: Context, line: str) -> None:
        """Displays the HTML DAG."""
        args = parse_argstring(self.dag, line)
        dag = context.get_dag(args.select_model)
        if args.file:
            with open(args.file, "w", encoding="utf-8") as file:
                file.write(str(dag))
        # TODO: Have this go through console instead of calling display directly
        self.display(dag)

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def migrate(self, context: Context, line: str) -> None:
        """Migrate SQLMesh to the current running version."""
        context.migrate()
        context.console.log_success("Migration complete")

    @magic_arguments()
    @argument(
        "--strict",
        action="store_true",
        help="Raise an error if the external model is missing in the database",
    )
    @line_magic
    @pass_sqlmesh_context
    def create_external_models(self, context: Context, line: str) -> None:
        """Create a schema file containing external model schemas."""
        args = parse_argstring(self.create_external_models, line)
        context.create_external_models(strict=args.strict)

    @magic_arguments()
    @argument(
        "source_to_target",
        type=str,
        metavar="SOURCE:TARGET",
        help="Source and target in `SOURCE:TARGET` format",
    )
    @argument(
        "--on",
        type=str,
        nargs="*",
        help="The column to join on. Can be specified multiple times. The model grain will be used if not specified.",
    )
    @argument(
        "--skip-columns",
        type=str,
        nargs="*",
        help="The column(s) to skip when comparing the source and target table.",
    )
    @argument(
        "--model",
        type=str,
        help="The model to diff against when source and target are environments and not tables.",
    )
    @argument(
        "--where",
        type=str,
        help="An optional where statement to filter results.",
    )
    @argument(
        "--limit",
        type=int,
        default=20,
        help="The limit of the sample dataframe.",
    )
    @argument(
        "--show-sample",
        action="store_true",
        help="Show a sample of the rows that differ. With many columns, the output can be very wide.",
    )
    @argument(
        "--decimals",
        type=int,
        default=3,
        help="The number of decimal places to keep when comparing floating point columns. Default: 3",
    )
    @argument(
        "--select-model",
        type=str,
        nargs="*",
        help="Specify one or more models to data diff. Use wildcards to diff multiple models. Ex: '*' (all models with applied plan diffs), 'demo.model+' (this and downstream models), 'git:feature_branch' (models with direct modifications in this branch only)",
    )
    @argument(
        "--skip-grain-check",
        action="store_true",
        help="Disable the check for a primary key (grain) that is missing or is not unique.",
    )
    @argument(
        "--warn-grain-check",
        action="store_true",
        help="Warn if any selected model is missing a grain, and compute diffs for the remaining models.",
    )
    @argument(
        "--schema-diff-ignore-case",
        action="store_true",
        help="If set, when performing a schema diff the case of column names is ignored when matching between the two schemas. For example, 'col_a' in the source schema and 'COL_A' in the target schema will be treated as the same column.",
    )
    @line_magic
    @pass_sqlmesh_context
    def table_diff(self, context: Context, line: str) -> None:
        """Show the diff between two tables.

        Can either be two tables or two environments and a model.
        """
        args = parse_argstring(self.table_diff, line)
        source, target = args.source_to_target.split(":")
        select_models = {args.model} if args.model else args.select_model or None
        context.table_diff(
            source=source,
            target=target,
            on=args.on,
            skip_columns=args.skip_columns,
            select_models=select_models,
            where=args.where,
            limit=args.limit,
            show_sample=args.show_sample,
            decimals=args.decimals,
            skip_grain_check=args.skip_grain_check,
            warn_grain_check=args.warn_grain_check,
            schema_diff_ignore_case=args.schema_diff_ignore_case,
        )

    @magic_arguments()
    @argument(
        "model_name",
        nargs="?",
        type=str,
        help="The name of the model to get the table name for.",
    )
    @argument(
        "--environment",
        type=str,
        help="The environment to source the model version from.",
    )
    @argument(
        "--prod",
        action="store_true",
        help="If set, return the name of the physical table that will be used in production for the model version promoted in the target environment.",
    )
    @line_magic
    @pass_sqlmesh_context
    def table_name(self, context: Context, line: str) -> None:
        """Prints the name of the physical table for the given model."""
        args = parse_argstring(self.table_name, line)
        context.console.log_status_update(
            context.table_name(args.model_name, args.environment, args.prod)
        )

    @magic_arguments()
    @argument(
        "pipeline",
        nargs="?",
        type=str,
        help="The dlt pipeline to attach for this SQLMesh project.",
    )
    @argument(
        "--table",
        "-t",
        type=str,
        nargs="*",
        help="The specific dlt tables to refresh in the SQLMesh models.",
    )
    @argument(
        "--force",
        "-f",
        action="store_true",
        help="If set, existing models are overwritten with the new DLT tables.",
    )
    @argument(
        "--dlt-path",
        type=str,
        help="The directory where the DLT pipeline resides.",
    )
    @line_magic
    @pass_sqlmesh_context
    def dlt_refresh(self, context: Context, line: str) -> None:
        """Attaches to a DLT pipeline with the option to update specific or all missing tables in the SQLMesh project."""
        from sqlmesh.integrations.dlt import generate_dlt_models

        args = parse_argstring(self.dlt_refresh, line)
        sqlmesh_models = generate_dlt_models(
            context, args.pipeline, list(args.table or []), args.force, args.dlt_path
        )
        if sqlmesh_models:
            model_names = "\n".join([f"- {model_name}" for model_name in sqlmesh_models])
            context.console.log_success(f"Updated SQLMesh project with models:\n{model_names}")
        else:
            context.console.log_success("All SQLMesh models are up to date.")

    @magic_arguments()
    @argument(
        "--read",
        type=str,
        default="",
        help="The input dialect of the sql string.",
    )
    @argument(
        "--write",
        type=str,
        default="",
        help="The output dialect of the sql string.",
    )
    @line_cell_magic
    @pass_sqlmesh_context
    def rewrite(self, context: Context, line: str, sql: str) -> None:
        """Rewrite a sql expression with semantic references into an executable query.

        https://sqlmesh.readthedocs.io/en/latest/concepts/metrics/overview/
        """
        args = parse_argstring(self.rewrite, line)
        context.console.show_sql(
            context.rewrite(sql, args.read).sql(
                dialect=args.write or context.config.dialect, pretty=True
            )
        )

    @magic_arguments()
    @argument(
        "--transpile",
        "-t",
        type=str,
        help="Transpile project models to the specified dialect.",
    )
    @argument(
        "--check",
        action="store_true",
        help="Whether or not to check formatting (but not actually format anything).",
        default=None,
    )
    @argument(
        "--append-newline",
        action="store_true",
        help="Include a newline at the end of the output.",
        default=None,
    )
    @argument(
        "--no-rewrite-casts",
        action="store_true",
        help="Preserve the existing casts, without rewriting them to use the :: syntax.",
        default=None,
    )
    @format_arguments
    @line_magic
    @pass_sqlmesh_context
    def format(self, context: Context, line: str) -> bool:
        """Format all SQL models and audits."""
        format_opts = vars(parse_argstring(self.format, line))
        if format_opts.pop("no_rewrite_casts", None):
            format_opts["rewrite_casts"] = False

        return context.format(**{k: v for k, v in format_opts.items() if v is not None})

    @magic_arguments()
    @argument("environment", type=str, help="The environment to diff local state against.")
    @line_magic
    @pass_sqlmesh_context
    def diff(self, context: Context, line: str) -> None:
        """Show the diff between the local state and the target environment."""
        args = parse_argstring(self.diff, line)
        context.diff(args.environment)

    @magic_arguments()
    @argument("environment", type=str, help="The environment to invalidate.")
    @line_magic
    @pass_sqlmesh_context
    def invalidate(self, context: Context, line: str) -> None:
        """Invalidate the target environment, forcing its removal during the next run of the janitor process."""
        args = parse_argstring(self.invalidate, line)
        context.invalidate_environment(args.environment)

    @magic_arguments()
    @argument(
        "--ignore-ttl",
        action="store_true",
        help="Cleanup snapshots that are not referenced in any environment, regardless of when they're set to expire",
    )
    @line_magic
    @pass_sqlmesh_context
    def janitor(self, context: Context, line: str) -> None:
        """Run the janitor process to clean up old environments and expired snapshots."""
        args = parse_argstring(self.janitor, line)
        context.run_janitor(ignore_ttl=args.ignore_ttl)

    @magic_arguments()
    @argument("model", type=str)
    @argument(
        "--query",
        "-q",
        type=str,
        nargs="+",
        default=[],
        help="Queries that will be used to generate data for the model's dependencies.",
    )
    @argument(
        "--overwrite",
        "-o",
        action="store_true",
        help="When true, the fixture file will be overwritten in case it already exists.",
    )
    @argument(
        "--var",
        "-v",
        type=str,
        nargs="+",
        help="Key-value pairs that will define variables needed by the model.",
    )
    @argument(
        "--path",
        "-p",
        type=str,
        help="The file path corresponding to the fixture, relative to the test directory. "
        "By default, the fixture will be created under the test directory and the file "
        "name will be inferred based on the test's name.",
    )
    @argument(
        "--name",
        "-n",
        type=str,
        help="The name of the test that will be created. By default, it's inferred based on the model's name.",
    )
    @argument(
        "--include-ctes",
        action="store_true",
        help="When true, CTE fixtures will also be generated.",
    )
    @line_magic
    @pass_sqlmesh_context
    def create_test(self, context: Context, line: str) -> None:
        """Generate a unit test fixture for a given model."""
        args = parse_argstring(self.create_test, line)
        queries = iter(args.query)
        variables = iter(args.var) if args.var else None
        context.create_test(
            args.model,
            input_queries={k: v.strip('"') for k, v in dict(zip(queries, queries)).items()},
            overwrite=args.overwrite,
            variables=dict(zip(variables, variables)) if variables else None,
            path=args.path,
            name=args.name,
            include_ctes=args.include_ctes,
        )

    @magic_arguments()
    @argument("tests", nargs="*", type=str)
    @argument(
        "--pattern",
        "-k",
        nargs="*",
        type=str,
        help="Only run tests that match the pattern of substring.",
    )
    @argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Verbose output. Use -vv for very verbose.",
    )
    @argument(
        "--preserve-fixtures",
        action="store_true",
        help="Preserve the fixture tables in the testing database, useful for debugging.",
    )
    @line_magic
    @pass_sqlmesh_context
    def run_test(self, context: Context, line: str) -> None:
        """Run unit test(s)."""
        args = parse_argstring(self.run_test, line)

        context.test(
            match_patterns=args.pattern,
            tests=args.tests,
            verbosity=Verbosity(args.verbose),
            preserve_fixtures=args.preserve_fixtures,
            stream=StringIO(),  # consume the output instead of redirecting to stdout
        )

    @magic_arguments()
    @argument(
        "models", type=str, nargs="*", help="A model to audit. Multiple models can be audited."
    )
    @argument("--start", "-s", type=str, help="Start date to audit.")
    @argument("--end", "-e", type=str, help="End date to audit.")
    @argument("--execution-time", type=str, help="Execution time.")
    @line_magic
    @pass_sqlmesh_context
    def audit(self, context: Context, line: str) -> bool:
        """Run audit(s)"""
        args = parse_argstring(self.audit, line)
        return context.audit(
            models=args.models, start=args.start, end=args.end, execution_time=args.execution_time
        )

    @magic_arguments()
    @argument("environment", nargs="?", type=str, help="The environment to check intervals for.")
    @argument(
        "--no-signals",
        action="store_true",
        help="Disable signal checks and only show missing intervals.",
        default=False,
    )
    @argument(
        "--select-model",
        type=str,
        nargs="*",
        help="Select specific model changes that should be included in the plan.",
    )
    @argument("--start", "-s", type=str, help="Start date of intervals to check for.")
    @argument("--end", "-e", type=str, help="End date of intervals to check for.")
    @line_magic
    @pass_sqlmesh_context
    def check_intervals(self, context: Context, line: str) -> None:
        """Show missing intervals in an environment, respecting signals."""
        args = parse_argstring(self.check_intervals, line)

        context.console.show_intervals(
            context.check_intervals(
                environment=args.environment,
                no_signals=args.no_signals,
                select_models=args.select_model,
                start=args.start,
                end=args.end,
            )
        )

    @magic_arguments()
    @argument(
        "--skip-connection",
        action="store_true",
        help="Skip the connection test.",
        default=False,
    )
    @argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Verbose output. Use -vv for very verbose.",
    )
    @line_magic
    @pass_sqlmesh_context
    def info(self, context: Context, line: str) -> None:
        """Display SQLMesh project information."""
        args = parse_argstring(self.info, line)
        context.print_info(skip_connection=args.skip_connection, verbosity=Verbosity(args.verbose))

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def rollback(self, context: Context, line: str) -> None:
        """Rollback SQLMesh to the previous migration."""
        context.rollback()

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def clean(self, context: Context, line: str) -> None:
        """Clears the SQLMesh cache and any build artifacts."""
        context.clear_caches()
        context.console.log_success("SQLMesh cache and build artifacts cleared")

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def environments(self, context: Context, line: str) -> None:
        """Prints the list of SQLMesh environments with its expiry datetime."""
        context.print_environment_names()

    @magic_arguments()
    @argument(
        "--models",
        "--model",
        type=str,
        nargs="*",
        help="A model to lint. Multiple models can be linted. If no models are specified, every model will be linted.",
    )
    @line_magic
    @pass_sqlmesh_context
    def lint(self, context: Context, line: str) -> None:
        """Run linter for target model(s)"""
        args = parse_argstring(self.lint, line)
        context.lint_models(args.models)

    @magic_arguments()
    @line_magic
    @pass_sqlmesh_context
    def destroy(self, context: Context, line: str) -> None:
        """Removes all project resources, engine-managed objects, state tables and clears the SQLMesh cache."""
        context.destroy()


def register_magics() -> None:
    try:
        shell = get_ipython()  # type: ignore
        shell.register_magics(SQLMeshMagics)
    except NameError:
        pass
