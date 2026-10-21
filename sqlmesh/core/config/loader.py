from __future__ import annotations

import glob
import os
import typing as t
from pathlib import Path

from pydantic import ValidationError
from dotenv import load_dotenv
from sqlglot.helper import ensure_list

from sqlmesh.core import constants as c
from sqlmesh.core.config.common import (
    ALL_CONFIG_FILENAMES,
    YAML_CONFIG_FILENAMES,
    DBT_PROJECT_FILENAME,
)
from sqlmesh.core.config.model import ModelDefaultsConfig
from sqlmesh.core.config.root import Config
from sqlmesh.utils import env_vars, merge_dicts, sys_path
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.metaprogramming import import_python_file
from sqlmesh.utils.pydantic import validation_error_message
from sqlmesh.utils.yaml import load as yaml_load

C = t.TypeVar("C", bound=Config)


def load_configs(
    config: t.Optional[t.Union[str, C]],
    config_type: t.Type[C],
    paths: t.Union[str | Path, t.Iterable[str | Path]],
    sqlmesh_path: t.Optional[Path] = None,
    dotenv_path: t.Optional[Path] = None,
    **kwargs: t.Any,
) -> t.Dict[Path, C]:
    sqlmesh_path = sqlmesh_path or c.SQLMESH_PATH
    config = config or "config"

    absolute_paths = [
        Path(t.cast(t.Union[str, Path], p)).absolute()
        for path in ensure_list(paths)
        for p in (glob.glob(str(path)) or [str(path)])
    ]

    if dotenv_path and dotenv_path.exists() and dotenv_path.is_file():
        load_dotenv(dotenv_path=dotenv_path, override=True)
    else:
        for path in absolute_paths:
            env_file = path / ".env"
            if env_file.exists() and env_file.is_file():
                load_dotenv(dotenv_path=env_file, override=True)

    if not isinstance(config, str):
        if type(config) != config_type:
            config = convert_config_type(config, config_type)
        return {path: config for path in absolute_paths}

    config_env_vars = None
    personal_paths = [sqlmesh_path / name for name in YAML_CONFIG_FILENAMES]
    for path in personal_paths:
        if path.exists():
            config_env_vars = load_config_from_yaml(path).get("env_vars")
            if config_env_vars:
                break

    with env_vars(config_env_vars if config_env_vars else {}):
        return {
            path: load_config_from_paths(
                config_type,
                project_paths=[path / name for name in ALL_CONFIG_FILENAMES],
                personal_paths=personal_paths,
                config_name=config,
                **kwargs,
            )
            for path in absolute_paths
        }


def load_config_from_paths(
    config_type: t.Type[C],
    project_paths: t.Optional[t.List[Path]] = None,
    personal_paths: t.Optional[t.List[Path]] = None,
    config_name: str = "config",
    load_from_env: bool = True,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    **kwargs: t.Any,
) -> C:
    project_paths = project_paths or []
    personal_paths = personal_paths or []
    visited_folders: t.Set[Path] = set()
    python_config: t.Optional[C] = None
    non_python_configs = []

    if not project_paths or not any(path.exists() for path in project_paths):
        raise ConfigError(
            "SQLMesh project config could not be found. Point the cli to the project path with `sqlmesh -p`. If you haven't set up the SQLMesh project, run `sqlmesh init`."
        )

    yaml_config_path: t.Optional[Path] = None
    for path in [*project_paths, *personal_paths]:
        if not path.exists():
            continue

        if not path.is_file():
            raise ConfigError(f"Path '{path}' must be a file.")

        parent_path = path.parent
        if parent_path in visited_folders:
            raise ConfigError(f"Multiple configuration files found in folder '{parent_path}'.")
        visited_folders.add(parent_path)

        extension = path.name.split(".")[-1].lower()
        if extension in ("yml", "yaml"):
            if config_name != "config" and not python_config:
                raise ConfigError(
                    "YAML configs do not support multiple configs. Use Python instead.",
                )
            yaml_config_path = path.resolve()
            non_python_configs.append(load_config_from_yaml(path, variables))
        elif extension == "py":
            try:
                python_config = load_config_from_python_module(
                    config_type, path, config_name=config_name
                )
            except ValidationError as e:
                raise ConfigError(
                    validation_error_message(e, f"Invalid project config '{config_name}':")
                    + "\n\nVerify your config.py."
                )
        else:
            raise ConfigError(
                f"Unsupported config file extension '{extension}' in config file '{path}'."
            )

    if load_from_env:
        env_config = load_config_from_env()
        if env_config:
            non_python_configs.append(load_config_from_env())

    if not non_python_configs and not python_config:
        raise ConfigError(
            "SQLMesh config could not be found. Point the cli to the right path with `sqlmesh -p`. If you haven't set up SQLMesh, run `sqlmesh init`."
        )

    non_python_config_dict = merge_dicts(*non_python_configs)

    supported_model_defaults = ModelDefaultsConfig.all_fields()
    for default in non_python_config_dict.get("model_defaults", {}):
        if default not in supported_model_defaults:
            raise ConfigError(
                f"'{default}' is not a valid model default configuration key. Please remove it from the `model_defaults` specification in your config file."
            )

    try:
        non_python_config = config_type.parse_obj(non_python_config_dict)
    except ValidationError as e:
        raise ConfigError(
            validation_error_message(e, "Invalid project config:")
            + "\n\nVerify your config.yaml and environment variables.",
            location=yaml_config_path,
        )

    no_dialect_err_msg = "Default model SQL dialect is a required configuration parameter. Set it in the `model_defaults` `dialect` key in your config file."

    # if "dbt_project.yml" is present *and there was no python config already defined*,
    # create a basic one to ensure we are using the DBT loader.
    # any config within yaml files will get overlayed on top of it.
    if not python_config:
        potential_project_files = [f / DBT_PROJECT_FILENAME for f in visited_folders]
        dbt_project_file = next((f for f in potential_project_files if f.exists()), None)
        if dbt_project_file:
            from sqlmesh.dbt.loader import sqlmesh_config

            infer_state_schema_name = False
            if dbt := non_python_config.dbt:
                infer_state_schema_name = dbt.infer_state_schema_name

            dbt_python_config = sqlmesh_config(
                project_root=dbt_project_file.parent,
                profiles_dir=kwargs.pop("profiles_dir", None),
                dbt_profile_name=kwargs.pop("profile", None),
                dbt_target_name=kwargs.pop("target", None),
                variables=variables,
                threads=kwargs.pop("threads", None),
                infer_state_schema_name=infer_state_schema_name,
            )
            if type(dbt_python_config) != config_type:
                dbt_python_config = convert_config_type(dbt_python_config, config_type)

            python_config = dbt_python_config

    if python_config:
        model_defaults = python_config.model_defaults
        if model_defaults.dialect is None:
            raise ConfigError(no_dialect_err_msg)
        return python_config.update_with(non_python_config)

    model_defaults = non_python_config.model_defaults
    if model_defaults.dialect is None:
        raise ConfigError(no_dialect_err_msg)

    return non_python_config


def load_config_from_yaml(
    path: Path, variables: t.Optional[t.Dict[str, t.Any]] = None
) -> t.Dict[str, t.Any]:
    content = yaml_load(path, variables=variables)
    if not isinstance(content, dict):
        raise ConfigError(
            f"Invalid YAML configuration: expected a dictionary but got {type(content).__name__}. "
            f"Please check the YAML syntax in your config file.",
            location=path,
        )
    return content


def load_config_from_python_module(
    config_type: t.Type[C],
    module_path: Path,
    config_name: str = "config",
) -> C:
    try:
        with sys_path(module_path.parent):
            config_module = import_python_file(module_path, module_path.parent)
    except Exception as e:
        raise ConfigError(
            f"Failed to load config file: {e}",
            location=module_path,
        )

    try:
        config_obj = getattr(config_module, config_name)
    except AttributeError:
        raise ConfigError(f"Config '{config_name}' was not found.")

    if config_obj is None or not isinstance(config_obj, Config):
        raise ConfigError(
            f"Config needs to be a valid object of type sqlmesh.core.config.Config. Found `{config_obj}` instead at '{module_path}'.",
            module_path,
        )

    return (
        config_obj
        if type(config_obj) == config_type
        else convert_config_type(config_obj, config_type)
    )


def load_config_from_env() -> t.Dict[str, t.Any]:
    config_dict: t.Dict[str, t.Any] = {}

    for key, value in os.environ.items():
        key = key.lower()
        if key.startswith(f"{c.SQLMESH}__") and key != (c.DISABLE_SQLMESH_STATE_MIGRATION).lower():
            segments = key.split("__")[1:]
            if not segments or not segments[-1]:
                raise ConfigError(f"Invalid SQLMesh configuration variable '{key}'.")

            target_dict = config_dict
            for config_key in segments[:-1]:
                if config_key not in target_dict:
                    target_dict[config_key] = {}
                target_dict = target_dict[config_key]
            target_dict[segments[-1]] = value

    return config_dict


def convert_config_type(
    config_obj: Config,
    config_type: t.Type[C],
) -> C:
    return config_type.parse_obj(config_obj.dict(exclude_computed_fields=True))
