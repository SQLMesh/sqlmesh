import typing as t
import click
from datetime import datetime, timedelta, timezone
from pydantic import ValidationError
from sqlglot import exp, parse_one
from sqlmesh.core.config.connection import parse_connection_config
from sqlmesh.core.context import Context
from sqlmesh.utils.date import yesterday_ds


def generate_dlt_models_and_settings(
    pipeline_name: str,
    dialect: str,
    tables: t.Optional[t.List[str]] = None,
    dlt_path: t.Optional[str] = None,
) -> t.Tuple[t.Set[t.Tuple[str, str]], t.Optional[str], str]:
    """
    This function attaches to a DLT pipeline and retrieves the connection configs and
    SQLMesh models based on the tables present in the pipeline's default schema.

    Args:
        pipeline_name: The name of the DLT pipeline to attach to.
        dialect: The SQL dialect to use for generating SQLMesh models.
        tables: A list of table names to include.
        dlt_path: The path to the DLT pipelines working directory, where DLT stores
            pipeline state (by default ~/.dlt/pipelines).

    Returns:
        A tuple containing a set of the SQLMesh model definitions, the connection config and the start date.
    """

    import dlt
    from dlt.common.schema.utils import has_table_seen_data, is_complete_column
    from dlt.pipeline.exceptions import CannotRestorePipelineException

    try:
        pipeline = dlt.attach(pipeline_name=pipeline_name, pipelines_dir=dlt_path or "")
    except CannotRestorePipelineException as e:
        from pathlib import Path
        from dlt.common.pipeline import get_dlt_pipelines_dir

        searched_dir = dlt_path or get_dlt_pipelines_dir()
        msg = f"Could not attach to pipeline {pipeline_name}.\nSearched in: {searched_dir}\n{e}"
        if dlt_path and (Path(get_dlt_pipelines_dir()) / pipeline_name).exists():
            msg += (
                f"\nHint: A pipeline named '{pipeline_name}' exists in the default pipelines "
                f"working directory '{get_dlt_pipelines_dir()}'. Note that --dlt-path must "
                "point to the directory where DLT stores pipeline working state (by default "
                "~/.dlt/pipelines), not the directory containing your pipeline scripts. "
                "Try omitting --dlt-path."
            )
        raise click.ClickException(msg)

    schema = pipeline.default_schema
    dataset = pipeline.dataset_name

    # Get the start date from the load_ids
    storage_ids = list(pipeline._get_load_storage().list_loaded_packages())
    start_date = get_start_date(storage_ids)

    # Get the connection credentials
    db_type = pipeline.destination.to_name(pipeline.destination)
    if db_type == "filesystem":
        connection_config = None
    else:
        if dlt.__version__ >= "1.10.0":
            client = pipeline.destination_client()
        else:
            client = pipeline._sql_job_client(schema)  # type: ignore
        config = client.config
        credentials = config.credentials
        configs = {
            key: value
            for key in dir(credentials)
            if not key.startswith("_")
            and not callable(value := getattr(credentials, key))
            and value is not None
        }
        connection_config = format_config(configs, db_type)

    dlt_tables = {
        name: table
        for name, table in schema.tables.items()
        if (
            (has_table_seen_data(table) and not name.startswith(schema._dlt_tables_prefix))
            or name == schema.loads_table_name
        )
        and (name in tables if tables else True)
    }

    sqlmesh_models = set()
    for table_name, table in dlt_tables.items():
        dlt_columns = {}
        primary_key = []

        # is_complete_column returns true if column contains a name and a data type
        for col in filter(is_complete_column, table["columns"].values()):
            dlt_columns[col["name"]] = exp.DataType.build(str(col["data_type"]), dialect=dialect)
            if col.get("primary_key"):
                primary_key.append(str(col["name"]))

        load_id = next(
            (col for col in ["_dlt_load_id", "load_id"] if col in dlt_columns),
            None,
        )
        load_key = "c." + load_id if load_id else ""
        parent_table = None

        # Handling for nested tables: https://dlthub.com/docs/general-usage/destination-tables#nested-tables
        if not load_id:
            if (
                "_dlt_parent_id" in dlt_columns
                and (parent_table := table["parent"])
                and parent_table in dlt_tables
            ):
                load_key = "p._dlt_load_id"
                parent_table = dataset + "." + parent_table
            else:
                break

        column_types = [
            exp.cast(exp.column(column, table="c"), data_type, dialect=dialect)
            .as_(column)
            .sql(dialect=dialect)
            for column, data_type in dlt_columns.items()
            if isinstance(column, str)
        ]
        select_columns = (
            ",\n".join(f"  {column_name}" for column_name in column_types) if column_types else ""
        )

        grain = f"\n  grain ({', '.join(primary_key)})," if primary_key else ""
        incremental_model_name = f"{dataset}_sqlmesh.incremental_{table_name}"
        incremental_model_sql = generate_incremental_model(
            incremental_model_name,
            select_columns,
            grain,
            dataset + "." + table_name,
            dialect,
            load_key,
            parent_table,
        )
        sqlmesh_models.add((incremental_model_name, incremental_model_sql))

    return sqlmesh_models, connection_config, start_date


def generate_dlt_models(
    context: Context,
    pipeline_name: str,
    tables: t.List[str],
    force: bool,
    dlt_path: t.Optional[str] = None,
) -> t.List[str]:
    from sqlmesh.cli.project_init import _create_object_files

    sqlmesh_models, _, _ = generate_dlt_models_and_settings(
        pipeline_name=pipeline_name,
        dialect=context.config.dialect or "",
        tables=tables if tables else None,
        dlt_path=dlt_path,
    )

    if not tables and not force:
        existing_models = [m.name for m in context.models.values()]
        sqlmesh_models = {model for model in sqlmesh_models if model[0] not in existing_models}

    if sqlmesh_models:
        _create_object_files(
            context.path / "models",
            {model[0].split(".")[-1]: model[1] for model in sqlmesh_models},
            "sql",
        )
        return [model[0] for model in sqlmesh_models]
    return []


def generate_incremental_model(
    model_name: str,
    select_columns: str,
    grain: str,
    from_table: str,
    dialect: str,
    load_id: str,
    parent_table: t.Optional[str] = None,
) -> str:
    """Generate the SQL definition for an incremental model."""

    time_column = parse_one(f"to_timestamp(CAST({load_id} AS DOUBLE))").sql(dialect=dialect)

    from_clause = f"{from_table} as c"
    if parent_table:
        from_clause += f"""\nJOIN
  {parent_table} as p
ON
  c._dlt_parent_id = p._dlt_id"""

    return f"""MODEL (
  name {model_name},
  kind INCREMENTAL_BY_TIME_RANGE (
    time_column _dlt_load_time,
  ),{grain}
);

SELECT
{select_columns},
  {time_column} as _dlt_load_time
FROM
  {from_clause}
WHERE
  {time_column} BETWEEN @start_ts AND @end_ts
"""


def format_config(configs: t.Dict[str, str], db_type: str) -> str:
    """Generate a string for the gateway connection config."""
    config = {
        "type": db_type,
    }

    for key, value in configs.items():
        if key == "password":
            config[key] = f'"{value}"'
        elif key == "username":
            config["user"] = value
        else:
            config[key] = value

    # Validate the connection config fields
    invalid_fields = []
    try:
        parse_connection_config(config)
    except ValidationError as e:
        for error in e.errors():
            invalid_fields.append(error.get("loc", [])[0])

    return "\n".join(
        [f"      {key}: {value}" for key, value in config.items() if key not in invalid_fields]
    )


def get_start_date(load_ids: t.List[str]) -> str:
    """Convert the earliest load_id to UTC timestamp, subtract a day and format as 'YYYY-MM-DD'."""

    timestamps = [datetime.fromtimestamp(float(id), tz=timezone.utc) for id in load_ids]
    if timestamps:
        start_timestamp = min(timestamps) - timedelta(days=1)
        return start_timestamp.strftime("%Y-%m-%d")
    return yesterday_ds()
