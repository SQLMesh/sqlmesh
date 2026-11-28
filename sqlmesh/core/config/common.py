from __future__ import annotations

import typing as t
from enum import Enum
import re

from sqlglot import exp

from sqlmesh.utils import classproperty
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import field_validator

# Config files that can be present in the project dir
ALL_CONFIG_FILENAMES = ("config.py", "config.yml", "config.yaml", "sqlmesh.yml", "sqlmesh.yaml")

# For personal paths (~/.sqlmesh/) where python config is not supported
YAML_CONFIG_FILENAMES = tuple(n for n in ALL_CONFIG_FILENAMES if not n.endswith(".py"))

# Note: is here to prevent having to import from sqlmesh.dbt.loader which introduces a dependency
# on dbt-core in a native project
DBT_PROJECT_FILENAME = "dbt_project.yml"


class EnvironmentSuffixTarget(str, Enum):
    # Intended to create virtual environments in their own schemas, with names like "<model_schema_name>__<env name>". The view name is untouched.
    # For example, a model named 'sqlmesh_example.full_model' created in an environment called 'dev'
    # would have its virtual layer view created as 'sqlmesh_example__dev.full_model'
    SCHEMA = "schema"

    # Intended to create virtual environments in the same schema as their production counterparts by adjusting the table name.
    # For example, a model named 'sqlmesh_example.full_model' created in an environment called 'dev'
    # would have its virtual layer view created as "sqlmesh_example.full_model__dev"
    TABLE = "table"

    # Intended to create virtual environments in their own catalogs to preserve the schema and view name of the models
    # For example, a model named 'sqlmesh_example.full_model' created in an environment called 'dev'
    # with a default catalog of "warehouse" would have its virtual layer view created as "warehouse__dev.sqlmesh_example.full_model"
    # note: this only works for engines that can query across catalogs
    CATALOG = "catalog"

    @property
    def is_schema(self) -> bool:
        return self == EnvironmentSuffixTarget.SCHEMA

    @property
    def is_table(self) -> bool:
        return self == EnvironmentSuffixTarget.TABLE

    @property
    def is_catalog(self) -> bool:
        return self == EnvironmentSuffixTarget.CATALOG

    @classproperty
    def default(cls) -> EnvironmentSuffixTarget:
        return EnvironmentSuffixTarget.SCHEMA

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return str(self)


class VirtualEnvironmentMode(str, Enum):
    """Mode for virtual environment behavior.

    FULL: Use full virtual environment functionality with versioned table names and virtual layer updates.
    DEV_ONLY: Bypass virtual environments in production, using original unversioned model names.
    """

    FULL = "full"
    DEV_ONLY = "dev_only"

    @property
    def is_full(self) -> bool:
        return self == VirtualEnvironmentMode.FULL

    @property
    def is_dev_only(self) -> bool:
        return self == VirtualEnvironmentMode.DEV_ONLY

    @classproperty
    def default(cls) -> VirtualEnvironmentMode:
        return VirtualEnvironmentMode.FULL

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return str(self)


class TableNamingConvention(str, Enum):
    # Causes table names at the physical layer to follow the convention:
    # <schema-name>__<table-name>__<fingerprint>
    SCHEMA_AND_TABLE = "schema_and_table"

    # Causes table names at the physical layer to follow the convention:
    # <table-name>__<fingerprint>
    TABLE_ONLY = "table_only"

    # Takes the table name that would be returned from SCHEMA_AND_TABLE and wraps it in md5()
    # to generate a hash and prefixes the has with `sqlmesh_md5__`, for the following reasons:
    # - at a glance, you can still see it's managed by sqlmesh and that md5 was used to generate the hash
    # - unquoted identifiers that start with numbers can trip up DB engine parsers, so having a text prefix prevents this
    # This causes table names at the physical layer to follow the convention:
    # sqlmesh_md5__3b07384d113edec49eaa6238ad5ff00d
    HASH_MD5 = "hash_md5"

    @classproperty
    def default(cls) -> TableNamingConvention:
        return TableNamingConvention.SCHEMA_AND_TABLE

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return str(self)


def _concurrent_tasks_validator(v: t.Any) -> int:
    if isinstance(v, str):
        v = int(v)
    if not isinstance(v, int) or v <= 0:
        raise ConfigError(
            f"The number of concurrent tasks must be an integer value greater than 0. '{v}' was provided"
        )
    return v


concurrent_tasks_validator = field_validator(
    "backfill_concurrent_tasks",
    "ddl_concurrent_tasks",
    "concurrent_tasks",
    mode="before",
    check_fields=False,
)(_concurrent_tasks_validator)


def _http_headers_validator(v: t.Any) -> t.Any:
    if isinstance(v, dict):
        return [(key, value) for key, value in v.items()]
    return v


http_headers_validator = field_validator(
    "http_headers",
    mode="before",
    check_fields=False,
)(_http_headers_validator)


def _variables_validator(value: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Variables must be a dictionary, not {type(value)}")

    def _validate_type(v: t.Any) -> None:
        if isinstance(v, list):
            for item in v:
                _validate_type(item)
        elif isinstance(v, dict):
            for item in v.values():
                _validate_type(item)
        elif v is not None and not isinstance(v, (str, int, float, bool)):
            raise ConfigError(f"Unsupported variable value type: {type(v)}")

    _validate_type(value)
    return {k.lower(): v for k, v in value.items()}


variables_validator = field_validator(
    "variables",
    mode="before",
    check_fields=False,
)(_variables_validator)


def _virtual_environment_mode_validator(cls: t.Type, v: t.Any) -> t.Any:
    if v is None:
        return cls.model_fields["virtual_environment_mode"].default
    if isinstance(v, (exp.Identifier, exp.Var)):
        return v.name.lower()
    if isinstance(v, exp.Literal) and v.is_string:
        return v.this.lower()
    if isinstance(v, str) and not isinstance(v, VirtualEnvironmentMode):
        return v.lower()
    return v


virtual_environment_mode_validator = field_validator(
    "virtual_environment_mode",
    mode="before",
    check_fields=False,
)(_virtual_environment_mode_validator)


def compile_regex_mapping(value: t.Dict[str | re.Pattern, t.Any]) -> t.Dict[re.Pattern, t.Any]:
    """
    Utility function to compile a dict of { "string regex pattern" : "string value" } into { "<re.Pattern>": "string value" }
    """
    compiled_regexes = {}
    for k, v in value.items():
        try:
            compiled_regexes[re.compile(k)] = v
        except re.error:
            raise ConfigError(f"`{k}` is not a valid regular expression.")
    return compiled_regexes
