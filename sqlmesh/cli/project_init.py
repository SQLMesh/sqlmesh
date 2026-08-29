import typing as t
from enum import Enum
from pathlib import Path
from dataclasses import dataclass
from rich.prompt import Prompt
from rich.console import Console
from sqlmesh.integrations.dlt import generate_dlt_models_and_settings
from sqlmesh.utils.date import yesterday_ds
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.core.config.common import VirtualEnvironmentMode

from sqlmesh.core.config.common import DBT_PROJECT_FILENAME
from sqlmesh.core.config.connection import (
    CONNECTION_CONFIG_TO_TYPE,
    DIALECT_TO_TYPE,
    INIT_DISPLAY_INFO_TO_TYPE,
)


PRIMITIVES = (str, int, bool, float)


class ProjectTemplate(Enum):
    DEFAULT = "default"
    DBT = "dbt"
    EMPTY = "empty"
    DLT = "dlt"


class InitCliMode(Enum):
    DEFAULT = "default"
    FLOW = "flow"


def _gen_config(
    engine_type: t.Optional[str],
    settings: t.Optional[str],
    start: t.Optional[str],
    template: ProjectTemplate,
    cli_mode: InitCliMode,
    dialect: t.Optional[str] = None,
) -> str:
    project_dialect = dialect or DIALECT_TO_TYPE.get(engine_type)

    connection_settings = (
        settings
        or """      type: duckdb
      database: db.db"""
    )

    if not settings and template != ProjectTemplate.DBT:
        doc_link = "https://sqlmesh.readthedocs.io/en/stable/integrations/engines{engine_link}"
        engine_link = ""

        if engine_type in CONNECTION_CONFIG_TO_TYPE:
            required_fields = []
            non_required_fields = []

            for name, field in CONNECTION_CONFIG_TO_TYPE[engine_type].model_fields.items():
                field_name = field.alias or name

                default_value = field.get_default()

                if isinstance(default_value, Enum):
                    default_value = default_value.value
                elif not isinstance(default_value, PRIMITIVES):
                    default_value = ""

                required = field.is_required() or field_name == "type"
                option_str = f"      {'# ' if not required else ''}{field_name}: {default_value}\n"

                # specify the DuckDB database field so quickstart runs out of the box
                if engine_type == "duckdb" and field_name == "database":
                    option_str = "      database: db.db\n"
                    required = True

                if required:
                    required_fields.append(option_str)
                else:
                    non_required_fields.append(option_str)

            connection_settings = "".join(required_fields + non_required_fields)

            engine_link = f"/{engine_type}/#connection-options"

        connection_settings = (
            "      # For more information on configuring the connection to your execution engine, visit:\n"
            "      # https://sqlmesh.readthedocs.io/en/stable/reference/configuration/#connection\n"
            f"      # {doc_link.format(engine_link=engine_link)}\n{connection_settings}"
        )

    default_configs = {
        ProjectTemplate.DEFAULT: f"""# --- Gateway Connection ---
gateways:
  {engine_type}:
    connection:
{connection_settings}
default_gateway: {engine_type}

# --- Model Defaults ---
# https://sqlmesh.readthedocs.io/en/stable/reference/model_configuration/#model-defaults

model_defaults:
  dialect: {project_dialect}
  start: {start or yesterday_ds()} # Start date for backfill history
  cron: '@daily'    # Run models daily at 12am UTC (can override per model)

# --- Linting Rules ---
# Enforce standards for your team
# https://sqlmesh.readthedocs.io/en/stable/guides/linter/

linter:
  enabled: true
  rules:
    - ambiguousorinvalidcolumn
    - invalidselectstarexpansion
    - noambiguousprojections
""",
        ProjectTemplate.DBT: f"""# --- DBT-specific options ---
dbt:
  # This configuration ensures that each dbt target gets its own isolated state.
  # The inferred state schemas are named "sqlmesh_state_<profile name>_<target schema>", eg "sqlmesh_state_jaffle_shop_dev"
  # If this is undesirable, you may manually configure the gateway to use a specific state schema name
  # https://sqlmesh.readthedocs.io/en/stable/integrations/dbt/#selecting-a-different-state-connection
  infer_state_schema_name: True

# --- Virtual Data Environment Mode ---
# Enable Virtual Data Environments (VDE) for *development* environments.
# Note that the production environment in dbt projects is not virtual by default to maintain compatibility with existing tooling.
# https://sqlmesh.readthedocs.io/en/stable/guides/configuration/#virtual-data-environment-modes
virtual_environment_mode: {VirtualEnvironmentMode.DEV_ONLY.lower()}

# --- Plan Defaults ---
# https://sqlmesh.readthedocs.io/en/stable/reference/configuration/#plan
plan:
  # For Virtual Data Environments, this ensures that any changes are always considered against prod,
  # rather than the previous state of that environment
  always_recreate_environment: True

# --- Model Defaults ---
# https://sqlmesh.readthedocs.io/en/stable/reference/model_configuration/#model-defaults
model_defaults:
  start: {start or yesterday_ds()}
""",
    }

    default_configs[ProjectTemplate.EMPTY] = default_configs[ProjectTemplate.DEFAULT]
    default_configs[ProjectTemplate.DLT] = default_configs[ProjectTemplate.DEFAULT]

    flow_cli_mode = """
# FLOW: Minimal prompts, automatic changes, summary output
# https://sqlmesh.readthedocs.io/en/stable/reference/configuration/#plan

plan:
  no_diff: true             # Hide detailed text differences for changed models
  no_prompts: true          # No interactive prompts
  auto_apply: true          # Apply changes automatically

# --- Optional: Set a default target environment ---
# This is intended for local development to prevent users from accidentally applying plans to the prod environment.
# It is a development only config and should NOT be committed to your git repo.
# https://sqlmesh.readthedocs.io/en/stable/guides/configuration/#default-target-environment

# Uncomment the following line to use a default target environment derived from the logged in user's name.
# default_target_environment: dev_{{ user() }}

# Example usage:
# sqlmesh plan            # Automatically resolves to: sqlmesh plan dev_yourname
# sqlmesh plan prod       # Specify `prod` to apply changes to production
"""

    return default_configs[template] + (flow_cli_mode if cli_mode == InitCliMode.FLOW else "")


@dataclass
class ExampleObjects:
    sql_models: t.Dict[str, str]
    python_models: t.Dict[str, str]
    seeds: t.Dict[str, str]
    audits: t.Dict[str, str]
    tests: t.Dict[str, str]
    sql_macros: t.Dict[str, str]
    python_macros: t.Dict[str, str]


def _gen_example_objects(schema_name: str) -> ExampleObjects:
    sql_models: t.Dict[str, str] = {}
    python_models: t.Dict[str, str] = {}
    seeds: t.Dict[str, str] = {}
    audits: t.Dict[str, str] = {}
    tests: t.Dict[str, str] = {}
    sql_macros: t.Dict[str, str] = {}
    python_macros: t.Dict[str, str] = {"__init__": ""}

    full_model_name = f"{schema_name}.full_model"
    incremental_model_name = f"{schema_name}.incremental_model"
    seed_model_name = f"{schema_name}.seed_model"

    sql_models[full_model_name] = f"""MODEL (
  name {full_model_name},
  kind FULL,
  cron '@daily',
  grain item_id,
  audits (assert_positive_order_ids),
);

SELECT
  item_id,
  COUNT(DISTINCT id) AS num_orders,
FROM
  {incremental_model_name}
GROUP BY item_id
  """

    sql_models[incremental_model_name] = f"""MODEL (
  name {incremental_model_name},
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column event_date
  ),
  start '2020-01-01',
  cron '@daily',
  grain (id, event_date)
);

SELECT
  id,
  item_id,
  event_date,
FROM
  {seed_model_name}
WHERE
  event_date BETWEEN @start_date AND @end_date
  """

    sql_models[seed_model_name] = f"""MODEL (
  name {seed_model_name},
  kind SEED (
    path '../seeds/seed_data.csv'
  ),
  columns (
    id INTEGER,
    item_id INTEGER,
    event_date DATE
  ),
  grain (id, event_date)
);
  """

    seeds["seed_data"] = """id,item_id,event_date
1,2,2020-01-01
2,1,2020-01-01
3,3,2020-01-03
4,1,2020-01-04
5,1,2020-01-05
6,1,2020-01-06
7,1,2020-01-07
"""

    audits["assert_positive_order_ids"] = """AUDIT (
  name assert_positive_order_ids,
);

SELECT *
FROM @this_model
WHERE
  item_id < 0
  """

    tests["test_full_model"] = f"""test_example_full_model:
  model: {full_model_name}
  inputs:
    {incremental_model_name}:
      rows:
      - id: 1
        item_id: 1
      - id: 2
        item_id: 1
      - id: 3
        item_id: 2
  outputs:
    query:
      rows:
      - item_id: 1
        num_orders: 2
      - item_id: 2
        num_orders: 1
  """

    return ExampleObjects(
        sql_models=sql_models,
        python_models=python_models,
        seeds=seeds,
        audits=audits,
        tests=tests,
        python_macros=python_macros,
        sql_macros=sql_macros,
    )


def init_example_project(
    path: t.Union[str, Path],
    engine_type: t.Optional[str],
    dialect: t.Optional[str] = None,
    template: ProjectTemplate = ProjectTemplate.DEFAULT,
    pipeline: t.Optional[str] = None,
    dlt_path: t.Optional[str] = None,
    schema_name: str = "sqlmesh_example",
    cli_mode: InitCliMode = InitCliMode.DEFAULT,
    start: t.Optional[str] = None,
) -> Path:
    root_path = Path(path)

    config_path = root_path / "config.yaml"
    if template == ProjectTemplate.DBT:
        # name the config file `sqlmesh.yaml` to make it clear that within the context of all
        # the existing yaml files DBT project, this one specifically relates to configuring the sqlmesh engine
        config_path = root_path / "sqlmesh.yaml"

    audits_path = root_path / "audits"
    macros_path = root_path / "macros"
    models_path = root_path / "models"
    seeds_path = root_path / "seeds"
    tests_path = root_path / "tests"

    if config_path.exists():
        raise SQLMeshError(
            f"Found an existing config file '{config_path}'.\n\nPlease change to another directory or remove the existing file."
        )

    if template == ProjectTemplate.DBT and not Path(root_path, DBT_PROJECT_FILENAME).exists():
        raise SQLMeshError(
            "Required dbt project file 'dbt_project.yml' not found in the current directory.\n\nPlease add it or change directories before running `sqlmesh init` to set up your project."
        )

    engine_types = "', '".join(CONNECTION_CONFIG_TO_TYPE)
    if engine_type is None and template != ProjectTemplate.DBT:
        raise SQLMeshError(
            f"Missing `engine` argument to `sqlmesh init` - please specify a SQL engine for your project. Options: '{engine_types}'."
        )

    if engine_type and engine_type not in CONNECTION_CONFIG_TO_TYPE:
        raise SQLMeshError(
            f"Invalid engine '{engine_type}'. Please specify one of '{engine_types}'."
        )

    models: t.Set[t.Tuple[str, str]] = set()
    settings = None
    if engine_type and template == ProjectTemplate.DLT:
        project_dialect = dialect or DIALECT_TO_TYPE.get(engine_type)
        if pipeline and project_dialect:
            dlt_models, settings, start = generate_dlt_models_and_settings(
                pipeline_name=pipeline, dialect=project_dialect, dlt_path=dlt_path
            )
        else:
            raise SQLMeshError(
                "Please provide a DLT pipeline with the `--dlt-pipeline` flag to generate a SQLMesh project from DLT."
            )

    _create_config(config_path, engine_type, dialect, settings, start, template, cli_mode)
    if template == ProjectTemplate.DBT:
        return config_path

    _create_folders([audits_path, macros_path, models_path, seeds_path, tests_path])

    if template == ProjectTemplate.DLT:
        _create_object_files(
            models_path, {model[0].split(".")[-1]: model[1] for model in dlt_models}, "sql"
        )
        return config_path

    example_objects = _gen_example_objects(schema_name=schema_name)

    if template != ProjectTemplate.EMPTY:
        _create_object_files(models_path, example_objects.sql_models, "sql")
        _create_object_files(models_path, example_objects.python_models, "py")
        _create_object_files(seeds_path, example_objects.seeds, "csv")
        _create_object_files(audits_path, example_objects.audits, "sql")
        _create_object_files(tests_path, example_objects.tests, "yaml")
        _create_object_files(macros_path, example_objects.python_macros, "py")
        _create_object_files(macros_path, example_objects.sql_macros, "sql")

    return config_path


def _create_folders(target_folders: t.Sequence[Path]) -> None:
    for folder_path in target_folders:
        folder_path.mkdir(exist_ok=True)
        (folder_path / ".gitkeep").touch()


def _create_config(
    config_path: Path,
    engine_type: t.Optional[str],
    dialect: t.Optional[str],
    settings: t.Optional[str],
    start: t.Optional[str],
    template: ProjectTemplate,
    cli_mode: InitCliMode,
) -> None:
    project_config = _gen_config(engine_type, settings, start, template, cli_mode, dialect)

    _write_file(
        config_path,
        project_config,
    )


def _create_object_files(path: Path, object_dict: t.Dict[str, str], file_extension: str) -> None:
    for object_name, object_def in object_dict.items():
        # file name is table component of catalog.schema.table
        _write_file(path / f"{object_name.split('.')[-1]}.{file_extension}", object_def)


def _write_file(path: Path, payload: str) -> None:
    with open(path, "w", encoding="utf-8") as fd:
        fd.write(payload)


def interactive_init(
    path: Path,
    console: Console,
    project_template: t.Optional[ProjectTemplate] = None,
) -> t.Tuple[ProjectTemplate, t.Optional[str], t.Optional[InitCliMode]]:
    console.print("──────────────────────────────")
    console.print("Welcome to SQLMesh!")

    project_template = _init_template_prompt(console) if not project_template else project_template

    if project_template == ProjectTemplate.DBT:
        return (project_template, None, None)

    engine_type = _init_engine_prompt(console)
    cli_mode = _init_cli_mode_prompt(console)

    return (project_template, engine_type, cli_mode)


def _init_integer_prompt(
    console: Console, err_msg_entity: str, num_options: int, retry_func: t.Callable[[t.Any], t.Any]
) -> int:
    err_msg = "\nERROR: '{option_str}' is not a valid {err_msg_entity} number - please enter a number between 1 and {num_options} or exit with control+c\n"
    while True:
        option_str = Prompt.ask("Enter a number", console=console)

        value_error = False
        try:
            option_num = int(option_str)
        except ValueError:
            value_error = True

        if value_error or option_num < 1 or option_num > num_options:
            console.print(
                err_msg.format(
                    option_str=option_str, err_msg_entity=err_msg_entity, num_options=num_options
                ),
                style="red",
            )
            continue
        console.print("")
        return option_num


def _init_display_choices(values_dict: t.Dict[str, str], console: Console) -> t.Dict[int, str]:
    display_num_to_value = {}
    for i, value_str in enumerate(values_dict.keys()):
        console.print(f"    \\[{i + 1}] {' ' if i < 9 else ''}{value_str} {values_dict[value_str]}")
        display_num_to_value[i + 1] = value_str
    console.print("")
    return display_num_to_value


def _init_template_prompt(console: Console) -> ProjectTemplate:
    console.print("──────────────────────────────\n")
    console.print("What type of project do you want to set up?\n")

    # These are ordered for user display - do not reorder
    template_descriptions = {
        ProjectTemplate.DEFAULT.name: "- Create SQLMesh example project models and files",
        ProjectTemplate.DBT.value: "    - You have an existing dbt project and want to run it with SQLMesh",
        ProjectTemplate.EMPTY.name: "  - Create a SQLMesh configuration file and project directories only",
    }

    display_num_to_template = _init_display_choices(template_descriptions, console)

    template_num = _init_integer_prompt(
        console, "project type", len(template_descriptions), _init_template_prompt
    )

    return ProjectTemplate(display_num_to_template[template_num].lower())


def _init_engine_prompt(console: Console) -> str:
    console.print("──────────────────────────────\n")
    console.print("Choose your SQL engine:\n")

    # INIT_DISPLAY_INFO_TO_TYPE is a dict of {engine_type: (display_order, display_name)}
    DISPLAY_NAME_TO_TYPE = {v[1]: k for k, v in INIT_DISPLAY_INFO_TO_TYPE.items()}
    ordered_engine_display_names = {
        info[1]: "" for info in sorted(INIT_DISPLAY_INFO_TO_TYPE.values(), key=lambda x: x[0])
    }
    display_num_to_display_name = _init_display_choices(ordered_engine_display_names, console)

    engine_num = _init_integer_prompt(
        console, "engine", len(ordered_engine_display_names), _init_engine_prompt
    )

    return DISPLAY_NAME_TO_TYPE[display_num_to_display_name[engine_num]]


def _init_cli_mode_prompt(console: Console) -> InitCliMode:
    console.print("──────────────────────────────\n")
    console.print("Choose your SQLMesh CLI experience:\n")

    cli_mode_descriptions = {
        InitCliMode.DEFAULT.name: "- See and control every detail",
        InitCliMode.FLOW.name: "   - Automatically run changes and show summary output",
    }

    display_num_to_cli_mode = _init_display_choices(cli_mode_descriptions, console)

    cli_mode_num = _init_integer_prompt(
        console, "config", len(cli_mode_descriptions), _init_cli_mode_prompt
    )

    return InitCliMode(display_num_to_cli_mode[cli_mode_num].lower())
