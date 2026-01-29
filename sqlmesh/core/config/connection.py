from __future__ import annotations

import abc
import base64
import importlib
import logging
import os
import pathlib
import re
import typing as t
from enum import Enum
from functools import partial
from sys import version_info

import pydantic
from packaging import version
from pydantic import Field
from pydantic_core import from_json
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.helper import subclasses

from sqlmesh.core import engine_adapter
from sqlmesh.core.config.base import BaseConfig
from sqlmesh.core.config.common import (
    compile_regex_mapping,
    concurrent_tasks_validator,
    http_headers_validator,
)
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.engine_adapter.shared import CatalogSupport
from sqlmesh.utils import debug_mode_enabled, str_to_bool
from sqlmesh.utils.aws import validate_s3_uri
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import (
    ValidationInfo,
    field_validator,
    get_concrete_types_from_typehint,
    model_validator,
    validation_error_message,
)

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import Self

logger = logging.getLogger(__name__)

RECOMMENDED_STATE_SYNC_ENGINES = {
    "postgres",
    "gcp_postgres",
    "mysql",
    "mssql",
    "azuresql",
}
FORBIDDEN_STATE_SYNC_ENGINES = {
    # Do not support row-level operations
    "spark",
    "trino",
    # Nullable types are problematic
    "clickhouse",
}
MOTHERDUCK_TOKEN_REGEX = re.compile(r"(\?|\&)(motherduck_token=)(\S*)")
PASSWORD_REGEX = re.compile(r"(password=)(\S+)")
SUPPORTS_MSSQL_PYTHON_DRIVER = (version_info.major, version_info.minor) >= (3, 10)


def _get_engine_import_validator(
    import_name: str, engine_type: str, extra_name: t.Optional[str] = None, decorate: bool = True
) -> t.Callable:
    extra_name = extra_name or engine_type

    def validate(cls: t.Any, data: t.Any) -> t.Any:
        check_import = (
            str_to_bool(str(data.pop("check_import", True))) if isinstance(data, dict) else True
        )
        if not check_import:
            return data
        try:
            importlib.import_module(import_name)
        except ImportError:
            if debug_mode_enabled():
                raise

            logger.exception("Failed to import the engine library")

            raise ConfigError(
                f"Failed to import the '{engine_type}' engine library. This may be due to a missing "
                "or incompatible installation. Please ensure the required dependency is installed by "
                f'running: `pip install "sqlmesh[{extra_name}]"`. For more details, check the logs '
                "in the 'logs/' folder, or rerun the command with the '--debug' flag."
            )

        return data

    return model_validator(mode="before")(validate) if decorate else validate


class ConnectionConfig(abc.ABC, BaseConfig):
    type_: str
    DIALECT: t.ClassVar[str]
    DISPLAY_NAME: t.ClassVar[str]
    DISPLAY_ORDER: t.ClassVar[int]
    concurrent_tasks: int
    register_comments: bool
    pre_ping: bool
    pretty_sql: bool = False
    schema_differ_overrides: t.Optional[t.Dict[str, t.Any]] = None
    catalog_type_overrides: t.Optional[t.Dict[str, str]] = None

    # Whether to share a  single connection across threads or create a new connection per thread.
    shared_connection: t.ClassVar[bool] = False

    @property
    @abc.abstractmethod
    def _connection_kwargs_keys(self) -> t.Set[str]:
        """keywords that should be passed into the connection"""

    @property
    @abc.abstractmethod
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        """The engine adapter for this connection"""

    @property
    @abc.abstractmethod
    def _connection_factory(self) -> t.Callable:
        """A function that is called to return a connection object for the given Engine Adapter"""

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        """The static connection kwargs for this connection"""
        return {}

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        """kwargs that are for execution config only"""
        return {}

    @property
    def _cursor_init(self) -> t.Optional[t.Callable[[t.Any], None]]:
        """A function that is called to initialize the cursor"""
        return None

    @property
    def is_recommended_for_state_sync(self) -> bool:
        """Whether this engine is recommended for being used as a state sync for production state syncs"""
        return self.type_ in RECOMMENDED_STATE_SYNC_ENGINES

    @property
    def is_forbidden_for_state_sync(self) -> bool:
        """Whether this engine is forbidden from being used as a state sync"""
        return self.type_ in FORBIDDEN_STATE_SYNC_ENGINES

    @property
    def _connection_factory_with_kwargs(self) -> t.Callable[[], t.Any]:
        """A function that is called to return a connection object for the given Engine Adapter"""
        return partial(
            self._connection_factory,
            **{
                **self._static_connection_kwargs,
                **{k: v for k, v in self.dict().items() if k in self._connection_kwargs_keys},
            },
        )

    def connection_validator(self) -> t.Callable[[], None]:
        """A function that validates the connection configuration"""
        return self.create_engine_adapter().ping

    def create_engine_adapter(
        self, register_comments_override: bool = False, concurrent_tasks: t.Optional[int] = None
    ) -> EngineAdapter:
        """Returns a new instance of the Engine Adapter."""

        concurrent_tasks = concurrent_tasks or self.concurrent_tasks
        return self._engine_adapter(
            self._connection_factory_with_kwargs,
            multithreaded=concurrent_tasks > 1,
            default_catalog=self.get_catalog(),
            cursor_init=self._cursor_init,
            register_comments=register_comments_override or self.register_comments,
            pre_ping=self.pre_ping,
            pretty_sql=self.pretty_sql,
            shared_connection=self.shared_connection,
            schema_differ_overrides=self.schema_differ_overrides,
            catalog_type_overrides=self.catalog_type_overrides,
            **self._extra_engine_config,
        )

    def get_catalog(self) -> t.Optional[str]:
        """The catalog for this connection"""
        if hasattr(self, "catalog"):
            return self.catalog
        if hasattr(self, "database"):
            return self.database
        if hasattr(self, "db"):
            return self.db
        return None

    @model_validator(mode="before")
    @classmethod
    def _expand_json_strings_to_concrete_types(cls, data: t.Any) -> t.Any:
        """
        There are situations where a connection config class has a field that is some kind of complex type
        (eg a list of strings or a dict) but the value is being supplied from a source such as an environment variable

        When this happens, the value is supplied as a string rather than a Python object. We need some way
        of turning this string into the corresponding Python list or dict.

        Rather than doing this piecemeal on every config subclass, this provides a generic implementatation
        to identify fields that may be be supplied as JSON strings and handle them transparently
        """
        if data and isinstance(data, dict):
            for maybe_json_field_name in cls._get_list_and_dict_field_names():
                if (value := data.get(maybe_json_field_name)) and isinstance(value, str):
                    # crude JSON check as we dont want to try and parse every string we get
                    value = value.strip()
                    if value.startswith("{") or value.startswith("["):
                        data[maybe_json_field_name] = from_json(value)

        return data

    @classmethod
    def _get_list_and_dict_field_names(cls) -> t.Set[str]:
        field_names = set()
        for name, field in cls.model_fields.items():
            if field.annotation:
                field_types = get_concrete_types_from_typehint(field.annotation)

                # check if the field type is something that could concievably be supplied as a json string
                if any(ft is t for t in (list, tuple, set, dict) for ft in field_types):
                    field_names.add(name)

        return field_names


class DuckDBAttachOptions(BaseConfig):
    type: str
    path: str
    read_only: bool = False

    # DuckLake specific options
    data_path: t.Optional[str] = None
    encrypted: bool = False
    data_inlining_row_limit: t.Optional[int] = None
    metadata_schema: t.Optional[str] = None

    def to_sql(self, alias: str) -> str:
        options = []
        # 'duckdb' is actually not a supported type, but we'd like to allow it for
        # fully qualified attach options or integration testing, similar to duckdb-dbt
        if self.type not in ("duckdb", "ducklake", "motherduck"):
            options.append(f"TYPE {self.type.upper()}")
        if self.read_only:
            options.append("READ_ONLY")

        # DuckLake specific options
        path = self.path
        if self.type == "ducklake":
            if not path.startswith("ducklake:"):
                path = f"ducklake:{path}"
            if self.data_path is not None:
                options.append(f"DATA_PATH '{self.data_path}'")
            if self.encrypted:
                options.append("ENCRYPTED")
            if self.data_inlining_row_limit is not None:
                options.append(f"DATA_INLINING_ROW_LIMIT {self.data_inlining_row_limit}")
            if self.metadata_schema is not None:
                options.append(f"METADATA_SCHEMA '{self.metadata_schema}'")

        options_sql = f" ({', '.join(options)})" if options else ""
        alias_sql = ""
        # TODO: Add support for Postgres schema. Currently adding it blocks access to the information_schema

        # MotherDuck does not support aliasing
        alias_sql = (
            f" AS {alias}" if not (self.type == "motherduck" or self.path.startswith("md:")) else ""
        )
        return f"ATTACH IF NOT EXISTS '{path}'{alias_sql}{options_sql}"


class BaseDuckDBConnectionConfig(ConnectionConfig):
    """Common configuration for the DuckDB-based connections.

    Args:
        database: The optional database name. If not specified, the in-memory database will be used.
        catalogs: Key is the name of the catalog and value is the path.
        extensions: A list of autoloadable extensions to load.
        connector_config: A dictionary of configuration to pass into the duckdb connector.
        secrets: A list of dictionaries used to generate DuckDB secrets for authenticating with external services (e.g. S3).
        filesystems: A list of dictionaries used to register `fsspec` filesystems to the DuckDB cursor.
        concurrent_tasks: The maximum number of tasks that can use this connection concurrently.
        register_comments: Whether or not to register model comments with the SQL engine.
        pre_ping: Whether or not to pre-ping the connection before starting a new transaction to ensure it is still alive.
        token: The optional MotherDuck token. If not specified and a MotherDuck path is in the catalog, the user will be prompted to login with their web browser.
    """

    database: t.Optional[str] = None
    catalogs: t.Optional[t.Dict[str, t.Union[str, DuckDBAttachOptions]]] = None
    extensions: t.List[t.Union[str, t.Dict[str, t.Any]]] = []
    connector_config: t.Dict[str, t.Any] = {}
    secrets: t.Union[t.List[t.Dict[str, t.Any]], t.Dict[str, t.Dict[str, t.Any]]] = []
    filesystems: t.List[t.Dict[str, t.Any]] = []

    concurrent_tasks: int = 1
    register_comments: bool = True
    pre_ping: t.Literal[False] = False

    token: t.Optional[str] = None

    shared_connection: t.ClassVar[bool] = True

    _data_file_to_adapter: t.ClassVar[t.Dict[str, EngineAdapter]] = {}

    @model_validator(mode="before")
    def _validate_database_catalogs(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        db_path = data.get("database")
        if db_path and data.get("catalogs"):
            raise ConfigError(
                "Cannot specify both `database` and `catalogs`. Define all your catalogs in `catalogs` and have the first entry be the default catalog"
            )
        if isinstance(db_path, str) and db_path.startswith("md:"):
            raise ConfigError(
                "Please use connection type 'motherduck' without the `md:` prefix if you want to use a MotherDuck database as the single `database`."
            )

        return data

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.DuckDBEngineAdapter

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {"database"}

    @property
    def _connection_factory(self) -> t.Callable:
        import duckdb

        return duckdb.connect

    @property
    def _cursor_init(self) -> t.Optional[t.Callable[[t.Any], None]]:
        """A function that is called to initialize the cursor"""
        import duckdb
        from duckdb import BinderException

        def init(cursor: duckdb.DuckDBPyConnection) -> None:
            for extension in self.extensions:
                extension = extension if isinstance(extension, dict) else {"name": extension}

                install_command = f"INSTALL {extension['name']}"

                if extension.get("repository"):
                    install_command = f"{install_command} FROM {extension['repository']}"

                if extension.get("force_install"):
                    install_command = f"FORCE {install_command}"

                try:
                    cursor.execute(install_command)
                    cursor.execute(f"LOAD {extension['name']}")
                except Exception as e:
                    raise ConfigError(f"Failed to load extension {extension['name']}: {e}")

            if self.connector_config:
                option_names = list(self.connector_config)
                in_part = ",".join("?" for _ in range(len(option_names)))

                cursor.execute(
                    f"SELECT name, value FROM duckdb_settings() WHERE name IN ({in_part})",
                    option_names,
                )

                existing_values = {field: setting for field, setting in cursor.fetchall()}

                # only set connector_config items if the values differ from what is already set
                # trying to set options like 'temp_directory' even to the same value can throw errors like:
                # Not implemented Error: Cannot switch temporary directory after the current one has been used
                for field, setting in self.connector_config.items():
                    if existing_values.get(field) != setting:
                        try:
                            cursor.execute(f"SET {field} = '{setting}'")
                        except Exception as e:
                            raise ConfigError(
                                f"Failed to set connector config {field} to {setting}: {e}"
                            )

            if self.secrets:
                duckdb_version = duckdb.__version__
                if version.parse(duckdb_version) < version.parse("0.10.0"):
                    from sqlmesh.core.console import get_console

                    get_console().log_warning(
                        f"DuckDB version {duckdb_version} does not support secrets-based authentication (requires 0.10.0 or later).\n"
                        "To use secrets, please upgrade DuckDB. For older versions, configure legacy authentication via `connector_config`.\n"
                        "More info: https://duckdb.org/docs/stable/extensions/httpfs/s3api_legacy_authentication.html"
                    )
                else:
                    if isinstance(self.secrets, list):
                        secrets_items = [(secret_dict, "") for secret_dict in self.secrets]
                    else:
                        secrets_items = [
                            (secret_dict, secret_name)
                            for secret_name, secret_dict in self.secrets.items()
                        ]

                    for secret_dict, secret_name in secrets_items:
                        secret_settings: t.List[str] = []
                        for field, setting in secret_dict.items():
                            secret_settings.append(f"{field} '{setting}'")
                        if secret_settings:
                            secret_clause = ", ".join(secret_settings)
                            try:
                                cursor.execute(
                                    f"CREATE OR REPLACE SECRET {secret_name} ({secret_clause});"
                                )
                            except Exception as e:
                                raise ConfigError(f"Failed to create secret: {e}")

            if self.filesystems:
                from fsspec import filesystem  # type: ignore

                for file_system in self.filesystems:
                    options = file_system.copy()
                    fs = options.pop("fs")
                    fs = filesystem(fs, **options)
                    cursor.register_filesystem(fs)

            for i, (alias, path_options) in enumerate(
                (getattr(self, "catalogs", None) or {}).items()
            ):
                # we parse_identifier and generate to ensure that `alias` has exactly one set of quotes
                # regardless of whether it comes in quoted or not
                alias = exp.parse_identifier(alias, dialect="duckdb").sql(
                    identify=True, dialect="duckdb"
                )
                try:
                    if isinstance(path_options, DuckDBAttachOptions):
                        query = path_options.to_sql(alias)
                    else:
                        query = f"ATTACH IF NOT EXISTS '{path_options}'"
                        if not path_options.startswith("md:"):
                            query += f" AS {alias}"
                    cursor.execute(query)
                except BinderException as e:
                    # If a user tries to create a catalog pointing at `:memory:` and with the name `memory`
                    # then we don't want to raise since this happens by default. They are just doing this to
                    # set it as the default catalog.
                    # If a user tried to attach a MotherDuck database/share which has already by attached via
                    # `ATTACH 'md:'`, then we don't want to raise since this is expected.
                    if (
                        not (
                            'database with name "memory" already exists' in str(e)
                            and path_options == ":memory:"
                        )
                        and f"""database with name "{path_options.path.replace("md:", "")}" already exists"""
                        not in str(e)
                    ):
                        raise e
                if i == 0 and not getattr(self, "database", None):
                    cursor.execute(f"USE {alias}")

        return init

    def create_engine_adapter(
        self, register_comments_override: bool = False, concurrent_tasks: t.Optional[int] = None
    ) -> EngineAdapter:
        """Checks if another engine adapter has already been created that shares a catalog that points to the same data
        file. If so, it uses that same adapter instead of creating a new one. As a result, any additional configuration
        associated with the new adapter will be ignored."""
        data_files = set((self.catalogs or {}).values())
        if self.database:
            if isinstance(self, MotherDuckConnectionConfig):
                data_files.add(
                    f"md:{self.database}"
                    + (f"?motherduck_token={self.token}" if self.token else "")
                )
            else:
                data_files.add(self.database)
        data_files.discard(":memory:")
        for data_file in data_files:
            key = data_file if isinstance(data_file, str) else data_file.path
            adapter = BaseDuckDBConnectionConfig._data_file_to_adapter.get(key)
            if adapter is not None:
                logger.info(
                    f"Using existing DuckDB adapter due to overlapping data file: {self._mask_sensitive_data(key)}"
                )
                return adapter

        if data_files:
            masked_files = {
                self._mask_sensitive_data(file if isinstance(file, str) else file.path)
                for file in data_files
            }
            logger.info(f"Creating new DuckDB adapter for data files: {masked_files}")
        else:
            logger.info("Creating new DuckDB adapter for in-memory database")
        adapter = super().create_engine_adapter(
            register_comments_override, concurrent_tasks=concurrent_tasks
        )
        for data_file in data_files:
            key = data_file if isinstance(data_file, str) else data_file.path
            BaseDuckDBConnectionConfig._data_file_to_adapter[key] = adapter
        return adapter

    def get_catalog(self) -> t.Optional[str]:
        if self.database:
            # Remove `:` from the database name in order to handle if `:memory:` is passed in
            return pathlib.Path(self.database.replace(":memory:", "memory")).stem
        if self.catalogs:
            return list(self.catalogs)[0]
        return None

    def _mask_sensitive_data(self, string: str) -> str:
        # Mask MotherDuck tokens with fixed number of asterisks
        result = MOTHERDUCK_TOKEN_REGEX.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{'*' * 8 if m.group(3) else ''}", string
        )
        # Mask PostgreSQL/MySQL passwords with fixed number of asterisks
        result = PASSWORD_REGEX.sub(lambda m: f"{m.group(1)}{'*' * 8}", result)
        return result


class MotherDuckConnectionConfig(BaseDuckDBConnectionConfig):
    """Configuration for the MotherDuck connection."""

    type_: t.Literal["motherduck"] = Field(alias="type", default="motherduck")
    DIALECT: t.ClassVar[t.Literal["duckdb"]] = "duckdb"
    DISPLAY_NAME: t.ClassVar[t.Literal["MotherDuck"]] = "MotherDuck"
    DISPLAY_ORDER: t.ClassVar[t.Literal[5]] = 5

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return set()

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        """kwargs that are for execution config only"""
        from sqlmesh import __version__

        custom_user_agent_config = {"custom_user_agent": f"SQLMesh/{__version__}"}
        connection_str = "md:"
        if self.database:
            # Attach single MD database instead of all databases on the account
            connection_str += f"{self.database}?attach_mode=single"
        if self.token:
            connection_str += f"{'&' if self.database else '?'}motherduck_token={self.token}"
        return {"database": connection_str, "config": custom_user_agent_config}

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {"is_motherduck": True}


class DuckDBConnectionConfig(BaseDuckDBConnectionConfig):
    """Configuration for the DuckDB connection."""

    type_: t.Literal["duckdb"] = Field(alias="type", default="duckdb")
    DIALECT: t.ClassVar[t.Literal["duckdb"]] = "duckdb"
    DISPLAY_NAME: t.ClassVar[t.Literal["DuckDB"]] = "DuckDB"
    DISPLAY_ORDER: t.ClassVar[t.Literal[1]] = 1


class SnowflakeConnectionConfig(ConnectionConfig):
    """Configuration for the Snowflake connection.

    Args:
        account: The Snowflake account name.
        user: The Snowflake username.
        password: The Snowflake password.
        warehouse: The optional warehouse name.
        database: The optional database name.
        role: The optional role name.
        concurrent_tasks: The maximum number of tasks that can use this connection concurrently.
        authenticator: The optional authenticator name. Defaults to username/password authentication ("snowflake").
                       Options: https://github.com/snowflakedb/snowflake-connector-python/blob/e937591356c067a77f34a0a42328907fda792c23/src/snowflake/connector/network.py#L178-L183
        token: The optional oauth access token to use for authentication when authenticator is set to "oauth".
        private_key: The optional private key to use for authentication. Key can be Base64-encoded DER format (representing the key bytes), a plain-text PEM format, or bytes (Python config only). https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect#using-key-pair-authentication-key-pair-rotation
        private_key_path: The optional path to the private key to use for authentication. This would be used instead of `private_key`.
        private_key_passphrase: The optional passphrase to use to decrypt `private_key` or `private_key_path`. Keys can be created without encryption so only provide this if needed.
        register_comments: Whether or not to register model comments with the SQL engine.
        pre_ping: Whether or not to pre-ping the connection before starting a new transaction to ensure it is still alive.
        session_parameters: The optional session parameters to set for the connection.
        host: Host address for the connection.
        port: Port for the connection.
    """

    account: str
    user: t.Optional[str] = None
    password: t.Optional[str] = None
    warehouse: t.Optional[str] = None
    database: t.Optional[str] = None
    role: t.Optional[str] = None
    authenticator: t.Optional[str] = None
    token: t.Optional[str] = None
    host: t.Optional[str] = None
    port: t.Optional[int] = None
    application: t.Literal["Tobiko_SQLMesh"] = "Tobiko_SQLMesh"

    # Private Key Auth
    private_key: t.Optional[t.Union[str, bytes]] = None
    private_key_path: t.Optional[str] = None
    private_key_passphrase: t.Optional[str] = None

    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: bool = False

    session_parameters: t.Optional[dict] = None

    type_: t.Literal["snowflake"] = Field(alias="type", default="snowflake")
    DIALECT: t.ClassVar[t.Literal["snowflake"]] = "snowflake"
    DISPLAY_NAME: t.ClassVar[t.Literal["Snowflake"]] = "Snowflake"
    DISPLAY_ORDER: t.ClassVar[t.Literal[2]] = 2

    _concurrent_tasks_validator = concurrent_tasks_validator

    @model_validator(mode="before")
    def _validate_authenticator(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        from snowflake.connector.network import DEFAULT_AUTHENTICATOR, OAUTH_AUTHENTICATOR

        auth = data.get("authenticator")
        auth = auth.upper() if auth else DEFAULT_AUTHENTICATOR
        user = data.get("user")
        password = data.get("password")
        data["private_key"] = cls._get_private_key(data, auth)  # type: ignore

        if (
            auth == DEFAULT_AUTHENTICATOR
            and not data.get("private_key")
            and (not user or not password)
        ):
            raise ConfigError("User and password must be provided if using default authentication")

        if auth == OAUTH_AUTHENTICATOR and not data.get("token"):
            raise ConfigError("Token must be provided if using oauth authentication")

        return data

    _engine_import_validator = _get_engine_import_validator(
        "snowflake.connector.network", "snowflake"
    )

    @classmethod
    def _get_private_key(cls, values: t.Dict[str, t.Optional[str]], auth: str) -> t.Optional[bytes]:
        """
        source: https://github.com/dbt-labs/dbt-snowflake/blob/0374b4ec948982f2ac8ec0c95d53d672ad19e09c/dbt/adapters/snowflake/connections.py#L247C5-L285C1

        Overall code change: Use local variables instead of class attributes + Validation
        """
        # Start custom code
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from snowflake.connector.network import (
            DEFAULT_AUTHENTICATOR,
            KEY_PAIR_AUTHENTICATOR,
        )

        private_key = values.get("private_key")
        private_key_path = values.get("private_key_path")
        private_key_passphrase = values.get("private_key_passphrase")
        user = values.get("user")
        password = values.get("password")
        auth = auth if auth and auth != DEFAULT_AUTHENTICATOR else KEY_PAIR_AUTHENTICATOR

        if not private_key and not private_key_path:
            return None
        if private_key and private_key_path:
            raise ConfigError("Cannot specify both `private_key` and `private_key_path`")
        if auth != KEY_PAIR_AUTHENTICATOR:
            raise ConfigError(
                f"Private key or private key path can only be provided when using {KEY_PAIR_AUTHENTICATOR} authentication"
            )
        if not user:
            raise ConfigError(
                f"User must be provided when using {KEY_PAIR_AUTHENTICATOR} authentication"
            )
        if password:
            raise ConfigError(
                f"Password cannot be provided when using {KEY_PAIR_AUTHENTICATOR} authentication"
            )

        if isinstance(private_key, bytes):
            return private_key
        # End Custom Code

        if private_key_passphrase:
            encoded_passphrase = private_key_passphrase.encode()
        else:
            encoded_passphrase = None

        if private_key:
            if private_key.startswith("-"):
                p_key = serialization.load_pem_private_key(
                    data=bytes(private_key, "utf-8"),
                    password=encoded_passphrase,
                    backend=default_backend(),
                )

            else:
                p_key = serialization.load_der_private_key(
                    data=base64.b64decode(private_key),
                    password=encoded_passphrase,
                    backend=default_backend(),
                )

        elif private_key_path:
            with open(private_key_path, "rb") as key:
                p_key = serialization.load_pem_private_key(
                    key.read(), password=encoded_passphrase, backend=default_backend()
                )
        else:
            return None

        return p_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "user",
            "password",
            "account",
            "warehouse",
            "database",
            "role",
            "authenticator",
            "token",
            "private_key",
            "session_parameters",
            "application",
            "host",
            "port",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.SnowflakeEngineAdapter

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        return {"autocommit": False}

    @property
    def _connection_factory(self) -> t.Callable:
        from snowflake import connector

        return connector.connect


class DatabricksConnectionConfig(ConnectionConfig):
    """
    Databricks connection that uses the SQL connector for SQL models and then Databricks Connect for Dataframe operations

    Arg Source: https://github.com/databricks/databricks-sql-python/blob/main/src/databricks/sql/client.py#L39
    OAuth ref: https://docs.databricks.com/en/dev-tools/python-sql-connector.html#oauth-machine-to-machine-m2m-authentication

    Args:
        server_hostname: Databricks instance host name.
        http_path: Http path either to a DBSQL endpoint (e.g. /sql/1.0/endpoints/1234567890abcdef)
            or to a DBR interactive cluster (e.g. /sql/protocolv1/o/1234567890123456/1234-123456-slid123)
        access_token: Http Bearer access token, e.g. Databricks Personal Access Token.
        auth_type: Set to 'databricks-oauth' or 'azure-oauth' to trigger OAuth (or dont set at all to use `access_token`)
        oauth_client_id: Client ID to use when auth_type is set to one of the 'oauth' types
        oauth_client_secret: Client Secret to use when auth_type is set to one of the 'oauth' types
        catalog: Default catalog to use for SQL models. Defaults to None which means it will use the default set in
            the Databricks cluster (most likely `hive_metastore`).
        http_headers: An optional list of (k, v) pairs that will be set as Http headers on every request
        session_configuration: An optional dictionary of Spark session parameters.
            Execute the SQL command `SET -v` to get a full list of available commands.
        databricks_connect_server_hostname: The hostname to use when establishing a connecting using Databricks Connect.
            Defaults to the `server_hostname` value.
        databricks_connect_access_token: The access token to use when establishing a connecting using Databricks Connect.
            Defaults to the `access_token` value.
        databricks_connect_cluster_id: The cluster id to use when establishing a connecting using Databricks Connect.
            Defaults to deriving the cluster id from the `http_path` value.
        force_databricks_connect: Force all queries to run using Databricks Connect instead of the SQL connector.
        disable_databricks_connect: Even if databricks connect is installed, do not use it.
        disable_spark_session: Do not use SparkSession if it is available (like when running in a notebook).
        pre_ping: Whether or not to pre-ping the connection before starting a new transaction to ensure it is still alive.
    """

    server_hostname: t.Optional[str] = None
    http_path: t.Optional[str] = None
    access_token: t.Optional[str] = None
    auth_type: t.Optional[str] = None
    oauth_client_id: t.Optional[str] = None
    oauth_client_secret: t.Optional[str] = None
    catalog: t.Optional[str] = None
    http_headers: t.Optional[t.List[t.Tuple[str, str]]] = None
    session_configuration: t.Optional[t.Dict[str, t.Any]] = None
    databricks_connect_server_hostname: t.Optional[str] = None
    databricks_connect_access_token: t.Optional[str] = None
    databricks_connect_cluster_id: t.Optional[str] = None
    databricks_connect_use_serverless: bool = False
    force_databricks_connect: bool = False
    disable_databricks_connect: bool = False
    disable_spark_session: bool = False

    concurrent_tasks: int = 1
    register_comments: bool = True
    pre_ping: t.Literal[False] = False

    type_: t.Literal["databricks"] = Field(alias="type", default="databricks")
    DIALECT: t.ClassVar[t.Literal["databricks"]] = "databricks"
    DISPLAY_NAME: t.ClassVar[t.Literal["Databricks"]] = "Databricks"
    DISPLAY_ORDER: t.ClassVar[t.Literal[3]] = 3

    _concurrent_tasks_validator = concurrent_tasks_validator
    _http_headers_validator = http_headers_validator

    @model_validator(mode="before")
    def _databricks_connect_validator(cls, data: t.Any) -> t.Any:
        # SQLQueryContextLogger will output any error SQL queries even if they are in a try/except block.
        # Disabling this allows SQLMesh to determine what should be shown to the user.
        # Ex: We describe a table to see if it exists and therefore that execution can fail but we don't need to show
        # the user since it is expected if the table doesn't exist. Without this change the user would see the error.
        logging.getLogger("SQLQueryContextLogger").setLevel(logging.CRITICAL)

        if not isinstance(data, dict):
            return data

        from sqlmesh.core.engine_adapter.databricks import DatabricksEngineAdapter

        if DatabricksEngineAdapter.can_access_spark_session(
            bool(data.get("disable_spark_session"))
        ):
            return data

        databricks_connect_use_serverless = data.get("databricks_connect_use_serverless")
        server_hostname, http_path, access_token, auth_type = (
            data.get("server_hostname"),
            data.get("http_path"),
            data.get("access_token"),
            data.get("auth_type"),
        )

        if (not server_hostname or not http_path or not access_token) and (
            not databricks_connect_use_serverless and not auth_type
        ):
            raise ValueError(
                "`server_hostname`, `http_path`, and `access_token` are required for Databricks connections when not running in a notebook"
            )
        if (
            databricks_connect_use_serverless
            and not server_hostname
            and not data.get("databricks_connect_server_hostname")
        ):
            raise ValueError(
                "`server_hostname` or `databricks_connect_server_hostname` is required when `databricks_connect_use_serverless` is set"
            )
        if DatabricksEngineAdapter.can_access_databricks_connect(
            bool(data.get("disable_databricks_connect"))
        ):
            if not data.get("databricks_connect_access_token"):
                data["databricks_connect_access_token"] = access_token
            if not data.get("databricks_connect_server_hostname"):
                data["databricks_connect_server_hostname"] = f"https://{server_hostname}"
            if not databricks_connect_use_serverless and not data.get(
                "databricks_connect_cluster_id"
            ):
                if t.TYPE_CHECKING:
                    assert http_path is not None
                data["databricks_connect_cluster_id"] = http_path.split("/")[-1]

        if auth_type:
            from databricks.sql.auth.auth import AuthType

            all_data = [m.value for m in AuthType]
            if auth_type not in all_data:
                raise ValueError(
                    f"`auth_type` {auth_type} does not match a valid option: {all_data}"
                )

            client_id = data.get("oauth_client_id")
            client_secret = data.get("oauth_client_secret")

            if client_secret and not client_id:
                raise ValueError(
                    "`oauth_client_id` is required when `oauth_client_secret` is specified"
                )

            if not http_path:
                raise ValueError("`http_path` is still required when using `auth_type`")

        return data

    _engine_import_validator = _get_engine_import_validator("databricks", "databricks")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        if self.use_spark_session_only:
            return set()
        return {
            "server_hostname",
            "http_path",
            "access_token",
            "http_headers",
            "session_configuration",
            "catalog",
        }

    @property
    def _engine_adapter(self) -> t.Type[engine_adapter.DatabricksEngineAdapter]:
        return engine_adapter.DatabricksEngineAdapter

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {
            k: v
            for k, v in self.dict().items()
            if k.startswith("databricks_connect_")
            or k in ("catalog", "disable_databricks_connect", "disable_spark_session")
        }

    @property
    def use_spark_session_only(self) -> bool:
        from sqlmesh.core.engine_adapter.databricks import DatabricksEngineAdapter

        return (
            DatabricksEngineAdapter.can_access_spark_session(self.disable_spark_session)
            or self.force_databricks_connect
        )

    @property
    def _connection_factory(self) -> t.Callable:
        if self.use_spark_session_only:
            from sqlmesh.engines.spark.db_api.spark_session import connection

            return connection

        from databricks import sql  # type: ignore

        return sql.connect

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        from sqlmesh.core.engine_adapter.databricks import DatabricksEngineAdapter

        if not self.use_spark_session_only:
            conn_kwargs: t.Dict[str, t.Any] = {
                "_user_agent_entry": "sqlmesh",
            }

            if self.auth_type and "oauth" in self.auth_type:
                # there are two types of oauth: User-to-Machine (U2M) and Machine-to-Machine (M2M)
                if self.oauth_client_secret:
                    # if a client_secret exists, then a client_id also exists and we are using M2M
                    # ref: https://docs.databricks.com/en/dev-tools/python-sql-connector.html#oauth-machine-to-machine-m2m-authentication
                    # ref: https://github.com/databricks/databricks-sql-python/blob/main/examples/m2m_oauth.py
                    from databricks.sdk.core import Config, oauth_service_principal

                    config = Config(
                        host=f"https://{self.server_hostname}",
                        client_id=self.oauth_client_id,
                        client_secret=self.oauth_client_secret,
                    )
                    conn_kwargs["credentials_provider"] = lambda: oauth_service_principal(config)
                else:
                    # if auth_type is set to an 'oauth' type but no client_id/secret are set, then we are using U2M
                    # ref: https://docs.databricks.com/en/dev-tools/python-sql-connector.html#oauth-user-to-machine-u2m-authentication
                    conn_kwargs["auth_type"] = self.auth_type

            return conn_kwargs

        if DatabricksEngineAdapter.can_access_spark_session(self.disable_spark_session):
            from pyspark.sql import SparkSession

            return dict(
                spark=SparkSession.getActiveSession(),
                catalog=self.catalog,
            )

        from databricks.connect import DatabricksSession

        if t.TYPE_CHECKING:
            assert self.databricks_connect_server_hostname is not None
            assert self.databricks_connect_access_token is not None

        if self.databricks_connect_use_serverless:
            builder = DatabricksSession.builder.remote(
                host=self.databricks_connect_server_hostname,
                token=self.databricks_connect_access_token,
                serverless=True,
            )
        else:
            if t.TYPE_CHECKING:
                assert self.databricks_connect_cluster_id is not None
            builder = DatabricksSession.builder.remote(
                host=self.databricks_connect_server_hostname,
                token=self.databricks_connect_access_token,
                cluster_id=self.databricks_connect_cluster_id,
            )

        return dict(
            spark=builder.userAgent("sqlmesh").getOrCreate(),
            catalog=self.catalog,
        )


class BigQueryConnectionMethod(str, Enum):
    OAUTH = "oauth"
    OAUTH_SECRETS = "oauth-secrets"
    SERVICE_ACCOUNT = "service-account"
    SERVICE_ACCOUNT_JSON = "service-account-json"


class BigQueryPriority(str, Enum):
    BATCH = "batch"
    INTERACTIVE = "interactive"

    @property
    def is_batch(self) -> bool:
        return self == self.BATCH

    @property
    def is_interactive(self) -> bool:
        return self == self.INTERACTIVE

    @property
    def bigquery_constant(self) -> str:
        from google.cloud.bigquery import QueryPriority

        if self.is_batch:
            return QueryPriority.BATCH
        return QueryPriority.INTERACTIVE


class BigQueryConnectionConfig(ConnectionConfig):
    """
    BigQuery Connection Configuration.
    """

    method: BigQueryConnectionMethod = BigQueryConnectionMethod.OAUTH

    project: t.Optional[str] = None
    execution_project: t.Optional[str] = None
    quota_project: t.Optional[str] = None
    location: t.Optional[str] = None
    # Keyfile Auth
    keyfile: t.Optional[str] = None
    keyfile_json: t.Optional[t.Dict[str, t.Any]] = None
    # Oath Secret Auth
    token: t.Optional[str] = None
    refresh_token: t.Optional[str] = None
    client_id: t.Optional[str] = None
    client_secret: t.Optional[str] = None
    token_uri: t.Optional[str] = None
    scopes: t.Tuple[str, ...] = ("https://www.googleapis.com/auth/bigquery",)
    impersonated_service_account: t.Optional[str] = None
    # Extra Engine Config
    job_creation_timeout_seconds: t.Optional[int] = None
    job_execution_timeout_seconds: t.Optional[int] = None
    job_retries: t.Optional[int] = 1
    job_retry_deadline_seconds: t.Optional[int] = None
    priority: t.Optional[BigQueryPriority] = None
    maximum_bytes_billed: t.Optional[int] = None

    concurrent_tasks: int = 1
    register_comments: bool = True
    pre_ping: t.Literal[False] = False

    type_: t.Literal["bigquery"] = Field(alias="type", default="bigquery")
    DIALECT: t.ClassVar[t.Literal["bigquery"]] = "bigquery"
    DISPLAY_NAME: t.ClassVar[t.Literal["BigQuery"]] = "BigQuery"
    DISPLAY_ORDER: t.ClassVar[t.Literal[4]] = 4

    _engine_import_validator = _get_engine_import_validator("google.cloud.bigquery", "bigquery")

    @field_validator("execution_project")
    def validate_execution_project(
        cls,
        v: t.Optional[str],
        info: ValidationInfo,
    ) -> t.Optional[str]:
        if v and not info.data.get("project"):
            raise ConfigError(
                "If the `execution_project` field is specified, you must also specify the `project` field to provide a default object location."
            )
        return v

    @field_validator("quota_project")
    def validate_quota_project(
        cls,
        v: t.Optional[str],
        info: ValidationInfo,
    ) -> t.Optional[str]:
        if v and not info.data.get("project"):
            raise ConfigError(
                "If the `quota_project` field is specified, you must also specify the `project` field to provide a default object location."
            )
        return v

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return set()

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.BigQueryEngineAdapter

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        """The static connection kwargs for this connection"""
        import google.auth
        from google.api_core import client_info, client_options
        from google.auth import impersonated_credentials
        from google.oauth2 import credentials, service_account

        if self.method == BigQueryConnectionMethod.OAUTH:
            creds, _ = google.auth.default(scopes=self.scopes)
        elif self.method == BigQueryConnectionMethod.SERVICE_ACCOUNT:
            creds = service_account.Credentials.from_service_account_file(
                self.keyfile, scopes=self.scopes
            )
        elif self.method == BigQueryConnectionMethod.SERVICE_ACCOUNT_JSON:
            creds = service_account.Credentials.from_service_account_info(
                self.keyfile_json, scopes=self.scopes
            )
        elif self.method == BigQueryConnectionMethod.OAUTH_SECRETS:
            creds = credentials.Credentials(
                token=self.token,
                refresh_token=self.refresh_token,
                client_id=self.client_id,
                client_secret=self.client_secret,
                token_uri=self.token_uri,
                scopes=self.scopes,
            )
        else:
            raise ConfigError("Invalid BigQuery Connection Method")

        if self.impersonated_service_account:
            creds = impersonated_credentials.Credentials(
                source_credentials=creds,
                target_principal=self.impersonated_service_account,
                target_scopes=self.scopes,
            )

        options = client_options.ClientOptions(quota_project_id=self.quota_project)
        project = self.execution_project or self.project or None

        client = google.cloud.bigquery.Client(
            project=project and exp.parse_identifier(project, dialect="bigquery").name,
            credentials=creds,
            location=self.location,
            client_info=client_info.ClientInfo(user_agent="sqlmesh"),
            client_options=options,
        )

        return {
            "client": client,
        }

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {
            k: v
            for k, v in self.dict().items()
            if k
            in {
                "job_creation_timeout_seconds",
                "job_execution_timeout_seconds",
                "job_retries",
                "job_retry_deadline_seconds",
                "priority",
                "maximum_bytes_billed",
            }
        }

    @property
    def _connection_factory(self) -> t.Callable:
        from google.cloud.bigquery.dbapi import connect

        return connect

    def get_catalog(self) -> t.Optional[str]:
        return self.project


class GCPPostgresConnectionConfig(ConnectionConfig):
    """
    Postgres Connection Configuration for GCP.

    Args:
        instance_connection_string: Connection name for the postgres instance.
        user: Postgres or IAM user's name
        password: The postgres user's password. Only needed when the user is a postgres user.
        enable_iam_auth: Set to True when user is an IAM user.
        db: Name of the db to connect to.
        keyfile: string path to json service account credentials file
        keyfile_json: dict service account credentials info
        pre_ping: Whether or not to pre-ping the connection before starting a new transaction to ensure it is still alive.
    """

    instance_connection_string: str
    user: str
    password: t.Optional[str] = None
    enable_iam_auth: t.Optional[bool] = None
    db: str
    ip_type: t.Union[t.Literal["public"], t.Literal["private"], t.Literal["psc"]] = "public"
    # Keyfile Auth
    keyfile: t.Optional[str] = None
    keyfile_json: t.Optional[t.Dict[str, t.Any]] = None
    timeout: t.Optional[int] = None
    scopes: t.Tuple[str, ...] = ("https://www.googleapis.com/auth/sqlservice.admin",)
    driver: str = "pg8000"

    type_: t.Literal["gcp_postgres"] = Field(alias="type", default="gcp_postgres")
    DIALECT: t.ClassVar[t.Literal["postgres"]] = "postgres"
    DISPLAY_NAME: t.ClassVar[t.Literal["GCP Postgres"]] = "GCP Postgres"
    DISPLAY_ORDER: t.ClassVar[t.Literal[13]] = 13

    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: bool = True

    _engine_import_validator = _get_engine_import_validator(
        "google.cloud.sql", "gcp_postgres", "gcppostgres"
    )

    @model_validator(mode="before")
    def _validate_auth_method(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        password = data.get("password")
        enable_iam_auth = data.get("enable_iam_auth")

        if not password and not enable_iam_auth:
            raise ConfigError(
                "GCP Postgres connection configuration requires either password set"
                " for a postgres user account or enable_iam_auth set to 'True'"
                " for an IAM user account."
            )

        return data

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "instance_connection_string",
            "driver",
            "user",
            "password",
            "db",
            "enable_iam_auth",
            "timeout",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.PostgresEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from google.cloud.sql.connector import Connector
        from google.oauth2 import service_account

        creds = None
        if self.keyfile:
            creds = service_account.Credentials.from_service_account_file(
                self.keyfile, scopes=self.scopes
            )
        elif self.keyfile_json:
            creds = service_account.Credentials.from_service_account_info(
                self.keyfile_json, scopes=self.scopes
            )

        kwargs = {
            "credentials": creds,
            "ip_type": self.ip_type,
        }

        if self.timeout:
            kwargs["timeout"] = self.timeout

        return Connector(**kwargs).connect  # type: ignore


class RedshiftConnectionConfig(ConnectionConfig):
    """
    Redshift Connection Configuration.

    Arg Source: https://github.com/aws/amazon-redshift-python-driver/blob/master/redshift_connector/__init__.py#L146
    Note: A subset of properties were selected. Please open an issue/PR if you want to see more supported.

    Args:
        user: The username to use for authentication with the Amazon Redshift cluster.
        password: The password to use for authentication with the Amazon Redshift cluster.
        database: The name of the database instance to connect to.
        host: The hostname of the Amazon Redshift cluster.
        port: The port number of the Amazon Redshift cluster. Default value is 5439.
        source_address: No description provided
        unix_sock: No description provided
        ssl: Is SSL enabled. Default value is ``True``. SSL must be enabled when authenticating using IAM.
        sslmode: The security of the connection to the Amazon Redshift cluster. 'verify-ca' and 'verify-full' are supported.
        timeout: The number of seconds before the connection to the server will timeout. By default there is no timeout.
        tcp_keepalive: Is `TCP keepalive <https://en.wikipedia.org/wiki/Keepalive#TCP_keepalive>`_ used. The default value is ``True``.
        application_name: Sets the application name. The default value is None.
        preferred_role: The IAM role preferred for the current connection.
        principal_arn: The ARN of the IAM entity (user or role) for which you are generating a policy.
        credentials_provider: The class name of the IdP that will be used for authenticating with the Amazon Redshift cluster.
        region: The AWS region where the Amazon Redshift cluster is located.
        cluster_identifier: The cluster identifier of the Amazon Redshift cluster.
        iam: If IAM authentication is enabled. Default value is False. IAM must be True when authenticating using an IdP.
        is_serverless: Redshift end-point is serverless or provisional. Default value false.
        serverless_acct_id: The account ID of the serverless. Default value None
        serverless_work_group: The name of work group for serverless end point. Default value None.
        pre_ping: Whether or not to pre-ping the connection before starting a new transaction to ensure it is still alive.
        enable_merge: Whether to use the Redshift merge operation instead of the SQLMesh logical merge.
    """

    user: t.Optional[str] = None
    password: t.Optional[str] = None
    database: t.Optional[str] = None
    host: t.Optional[str] = None
    port: t.Optional[int] = None
    source_address: t.Optional[str] = None
    unix_sock: t.Optional[str] = None
    ssl: t.Optional[bool] = None
    sslmode: t.Optional[str] = None
    timeout: t.Optional[int] = None
    tcp_keepalive: t.Optional[bool] = None
    application_name: t.Optional[str] = None
    preferred_role: t.Optional[str] = None
    principal_arn: t.Optional[str] = None
    credentials_provider: t.Optional[str] = None
    region: t.Optional[str] = None
    cluster_identifier: t.Optional[str] = None
    iam: t.Optional[bool] = None
    is_serverless: t.Optional[bool] = None
    serverless_acct_id: t.Optional[str] = None
    serverless_work_group: t.Optional[str] = None
    enable_merge: t.Optional[bool] = None

    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: bool = False

    type_: t.Literal["redshift"] = Field(alias="type", default="redshift")
    DIALECT: t.ClassVar[t.Literal["redshift"]] = "redshift"
    DISPLAY_NAME: t.ClassVar[t.Literal["Redshift"]] = "Redshift"
    DISPLAY_ORDER: t.ClassVar[t.Literal[7]] = 7

    _engine_import_validator = _get_engine_import_validator("redshift_connector", "redshift")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "user",
            "password",
            "database",
            "host",
            "port",
            "source_address",
            "unix_sock",
            "ssl",
            "sslmode",
            "timeout",
            "tcp_keepalive",
            "application_name",
            "preferred_role",
            "principal_arn",
            "credentials_provider",
            "region",
            "cluster_identifier",
            "iam",
            "is_serverless",
            "serverless_acct_id",
            "serverless_work_group",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.RedshiftEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from redshift_connector import connect

        return connect

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {"enable_merge": self.enable_merge}


class PostgresConnectionConfig(ConnectionConfig):
    host: str
    user: str
    password: str
    port: int
    database: str
    keepalives_idle: t.Optional[int] = None
    connect_timeout: int = 10
    role: t.Optional[str] = None
    sslmode: t.Optional[str] = None
    application_name: t.Optional[str] = None

    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: bool = True

    type_: t.Literal["postgres"] = Field(alias="type", default="postgres")
    DIALECT: t.ClassVar[t.Literal["postgres"]] = "postgres"
    DISPLAY_NAME: t.ClassVar[t.Literal["Postgres"]] = "Postgres"
    DISPLAY_ORDER: t.ClassVar[t.Literal[12]] = 12

    _engine_import_validator = _get_engine_import_validator("psycopg2", "postgres")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "host",
            "user",
            "password",
            "port",
            "database",
            "keepalives_idle",
            "connect_timeout",
            "sslmode",
            "application_name",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.PostgresEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from psycopg2 import connect

        return connect

    @property
    def _cursor_init(self) -> t.Optional[t.Callable[[t.Any], None]]:
        if not self.role:
            return None

        def init(cursor: t.Any) -> None:
            cursor.execute(f"SET ROLE {self.role}")

        return init


class MySQLConnectionConfig(ConnectionConfig):
    host: str
    user: str
    password: str
    port: t.Optional[int] = None
    database: t.Optional[str] = None
    charset: t.Optional[str] = None
    collation: t.Optional[str] = None
    ssl_disabled: t.Optional[bool] = None

    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: bool = True

    type_: t.Literal["mysql"] = Field(alias="type", default="mysql")
    DIALECT: t.ClassVar[t.Literal["mysql"]] = "mysql"
    DISPLAY_NAME: t.ClassVar[t.Literal["MySQL"]] = "MySQL"
    DISPLAY_ORDER: t.ClassVar[t.Literal[14]] = 14

    _engine_import_validator = _get_engine_import_validator("pymysql", "mysql")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        connection_keys = {
            "host",
            "user",
            "password",
        }
        if self.port is not None:
            connection_keys.add("port")
        if self.database is not None:
            connection_keys.add("database")
        if self.charset is not None:
            connection_keys.add("charset")
        if self.collation is not None:
            connection_keys.add("collation")
        if self.ssl_disabled is not None:
            connection_keys.add("ssl_disabled")
        return connection_keys

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.MySQLEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from pymysql import connect

        return connect


class MSSQLConnectionConfig(ConnectionConfig):
    host: str
    user: t.Optional[str] = None
    password: t.Optional[str] = None
    database: t.Optional[str] = ""
    timeout: t.Optional[int] = 0
    login_timeout: t.Optional[int] = 60
    charset: t.Optional[str] = "UTF-8"
    appname: t.Optional[str] = None
    port: t.Optional[int] = 1433
    conn_properties: t.Optional[t.Union[t.List[str], str]] = None
    autocommit: t.Optional[bool] = False
    tds_version: t.Optional[str] = None

    # Driver options
    driver: t.Literal["pymssql", "pyodbc", "mssql-python"] = "pymssql"
    # PyODBC specific options
    driver_name: t.Optional[str] = None  # e.g. "ODBC Driver 18 for SQL Server"
    trust_server_certificate: t.Optional[bool] = None
    encrypt: t.Optional[bool] = None
    # Dictionary of arbitrary ODBC connection properties
    # See: https://learn.microsoft.com/en-us/sql/connect/odbc/dsn-connection-string-attribute
    odbc_properties: t.Optional[t.Dict[str, t.Any]] = None

    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: bool = True

    type_: t.Literal["mssql"] = Field(alias="type", default="mssql")
    DIALECT: t.ClassVar[t.Literal["tsql"]] = "tsql"
    DISPLAY_NAME: t.ClassVar[t.Literal["MSSQL"]] = "MSSQL"
    DISPLAY_ORDER: t.ClassVar[t.Literal[11]] = 11

    @model_validator(mode="before")
    @classmethod
    def _mssql_engine_import_validator(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        driver = data.get("driver", "pymssql")

        # Define the mapping of driver to import module and extra name
        driver_configs = {
            "pymssql": ("pymssql", "mssql"),
            "pyodbc": ("pyodbc", "mssql-odbc"),
            "mssql-python": ("mssql_python", "mssql-python"),
        }

        if driver not in driver_configs:
            raise ValueError(f"Unsupported driver: {driver}")

        import_module, extra_name = driver_configs[driver]

        # Use _get_engine_import_validator with decorate=False to get the raw validation function
        # This avoids the __wrapped__ issue in Python 3.9
        validator_func = _get_engine_import_validator(
            import_module, driver, extra_name, decorate=False
        )

        # Call the raw validation function directly
        return validator_func(cls, data)

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        base_keys = {
            "host",
            "user",
            "password",
            "database",
            "timeout",
            "login_timeout",
            "charset",
            "appname",
            "port",
            "conn_properties",
            "autocommit",
            "tds_version",
        }

        if self.driver == "pyodbc":
            base_keys.update(
                {
                    "driver_name",
                    "trust_server_certificate",
                    "encrypt",
                    "odbc_properties",
                }
            )
            # Remove pymssql-specific parameters
            base_keys.discard("tds_version")
            base_keys.discard("conn_properties")

        elif self.driver == "mssql-python":
            base_keys.update(
                {
                    "trust_server_certificate",
                    "encrypt",
                    "odbc_properties",
                }
            )
            # Remove pymssql-specific parameters
            base_keys.discard("tds_version")
            base_keys.discard("conn_properties")

        return base_keys

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.MSSQLEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        if self.driver == "pymssql":
            import pymssql

            return pymssql.connect

        if self.driver == "mssql-python":
            # The `mssql-python` implementation is API-compatible with
            # with the `pyodbc` equivalent for documented parameters.

            if not SUPPORTS_MSSQL_PYTHON_DRIVER:
                raise ConfigError("The `mssql-python` driver requires Python 3.10 or higher.")

            import mssql_python

            def connect_mssql_python(**kwargs: t.Any) -> t.Callable:
                # Extract parameters for connection string
                host = kwargs.pop("host")
                port = kwargs.pop("port", 1433)
                database = kwargs.pop("database", "")
                user = kwargs.pop("user", None)
                password = kwargs.pop("password", None)
                authentication = kwargs.pop("authentication", None)
                trust_server_certificate = kwargs.pop("trust_server_certificate", False)
                encrypt = kwargs.pop("encrypt", True)
                login_timeout = kwargs.pop("login_timeout", 59)
                login_attempts = kwargs.pop("login_attempts", 1)  # TODO: document

                # Build connection string
                conn_str_parts = [
                    f"Server={host},{port}",
                ]

                if database:
                    conn_str_parts.append(f"Database={database}")

                # Add security options
                conn_str_parts.append(f"Encrypt={'yes' if encrypt else 'no'}")
                if trust_server_certificate:
                    conn_str_parts.append("TrustServerCertificate=yes")

                conn_str_parts.append(f"ConnectRetryCount={login_attempts}")
                conn_str_parts.append(f"ConnectRetryInterval={min(int(login_timeout), 60)}")

                # Standard SQL Server authentication
                if user:
                    conn_str_parts.append(f"UID={user}")
                if password:
                    conn_str_parts.append(f"PWD={password}")
                if authentication:
                    conn_str_parts.append(f"Authentication={authentication}")

                # Add any additional ODBC properties from the odbc_properties dictionary
                if self.odbc_properties:
                    for key, value in self.odbc_properties.items():
                        # Skip properties that we've already set above
                        if key.lower() in (
                            "driver",
                            "server",
                            "database",
                            "uid",
                            "pwd",
                            "encrypt",
                            "trustservercertificate",
                            "connection timeout",
                        ):
                            continue

                        # Handle boolean values properly
                        if isinstance(value, bool):
                            conn_str_parts.append(f"{key}={'yes' if value else 'no'}")
                        else:
                            conn_str_parts.append(f"{key}={value}")

                # Create the connection
                conn_str = ";".join(conn_str_parts)

                conn = mssql_python.connect(conn_str, autocommit=kwargs.get("autocommit", False))

                # TODO: Remove this output converter as DATETIMEOFFSET
                # should be handled natively by `mssql-python`.
                # see "https://github.com/microsoft/mssql-python/issues/213"

                def handle_datetimeoffset_mssql_python(dto_value: t.Any) -> t.Any:
                    import struct
                    from datetime import datetime, timedelta, timezone

                    # Unpack the DATETIMEOFFSET binary format:
                    # Format: <6hI2h = (year, month, day, hour, minute, second, nanoseconds, tz_hour_offset, tz_minute_offset)
                    tup = struct.unpack("<6hI2h", dto_value)
                    return datetime(
                        tup[0],
                        tup[1],
                        tup[2],
                        tup[3],
                        tup[4],
                        tup[5],
                        tup[6] // 1000,
                        timezone(timedelta(hours=tup[7], minutes=tup[8])),
                    )

                conn.add_output_converter(-155, handle_datetimeoffset_mssql_python)

                return conn

            return connect_mssql_python

        if self.driver == "pyodbc":

            def connect_pyodbc(**kwargs: t.Any) -> t.Callable:
                # Extract parameters for connection string
                host = kwargs.pop("host")
                port = kwargs.pop("port", 1433)
                database = kwargs.pop("database", "")
                user = kwargs.pop("user", None)
                password = kwargs.pop("password", None)
                driver_name = kwargs.pop("driver_name", "ODBC Driver 18 for SQL Server")
                trust_server_certificate = kwargs.pop("trust_server_certificate", False)
                encrypt = kwargs.pop("encrypt", True)
                login_timeout = kwargs.pop("login_timeout", 60)

                # Build connection string
                conn_str_parts = [
                    f"DRIVER={{{driver_name}}}",
                    f"SERVER={host},{port}",
                ]

                if database:
                    conn_str_parts.append(f"DATABASE={database}")

                # Add security options
                conn_str_parts.append(f"Encrypt={'YES' if encrypt else 'NO'}")
                if trust_server_certificate:
                    conn_str_parts.append("TrustServerCertificate=YES")

                conn_str_parts.append(f"Connection Timeout={login_timeout}")

                # Standard SQL Server authentication
                if user:
                    conn_str_parts.append(f"UID={user}")
                if password:
                    conn_str_parts.append(f"PWD={password}")

                # Add any additional ODBC properties from the odbc_properties dictionary
                if self.odbc_properties:
                    for key, value in self.odbc_properties.items():
                        # Skip properties that we've already set above
                        if key.lower() in (
                            "driver",
                            "server",
                            "database",
                            "uid",
                            "pwd",
                            "encrypt",
                            "trustservercertificate",
                            "connection timeout",
                        ):
                            continue

                        # Handle boolean values properly
                        if isinstance(value, bool):
                            conn_str_parts.append(f"{key}={'YES' if value else 'NO'}")
                        else:
                            conn_str_parts.append(f"{key}={value}")

                # Create the connection
                conn_str = ";".join(conn_str_parts)

                import pyodbc

                conn = pyodbc.connect(conn_str, autocommit=kwargs.get("autocommit", False))

                # Set up output converters for MSSQL-specific data types
                # Handle SQL type -155 (DATETIMEOFFSET) which is not yet supported by pyodbc
                # ref: https://github.com/mkleehammer/pyodbc/issues/134#issuecomment-281739794
                def handle_datetimeoffset_pyodbc(dto_value: t.Any) -> t.Any:
                    import struct
                    from datetime import datetime, timedelta, timezone

                    # Unpack the DATETIMEOFFSET binary format:
                    # Format: <6hI2h = (year, month, day, hour, minute, second, nanoseconds, tz_hour_offset, tz_minute_offset)
                    tup = struct.unpack("<6hI2h", dto_value)
                    return datetime(
                        tup[0],
                        tup[1],
                        tup[2],
                        tup[3],
                        tup[4],
                        tup[5],
                        tup[6] // 1000,
                        timezone(timedelta(hours=tup[7], minutes=tup[8])),
                    )

                conn.add_output_converter(-155, handle_datetimeoffset_pyodbc)

                return conn

            return connect_pyodbc

        raise ValueError(f"Unsupported driver: {self.driver}")

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {"catalog_support": CatalogSupport.REQUIRES_SET_CATALOG}


class AzureSQLConnectionConfig(MSSQLConnectionConfig):
    type_: t.Literal["azuresql"] = Field(alias="type", default="azuresql")  # type: ignore
    DISPLAY_NAME: t.ClassVar[t.Literal["Azure SQL"]] = "Azure SQL"  # type: ignore
    DISPLAY_ORDER: t.ClassVar[t.Literal[10]] = 10  # type: ignore

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {"catalog_support": CatalogSupport.SINGLE_CATALOG_ONLY}


class FabricConnectionConfig(MSSQLConnectionConfig):
    """
    Fabric Connection Configuration.
    Inherits most settings from MSSQLConnectionConfig and sets the type to 'fabric'.
    It is recommended to use the 'pyodbc' driver for Fabric.
    """

    type_: t.Literal["fabric"] = Field(alias="type", default="fabric")  # type: ignore
    DIALECT: t.ClassVar[t.Literal["fabric"]] = "fabric"  # type: ignore
    DISPLAY_NAME: t.ClassVar[t.Literal["Fabric"]] = "Fabric"  # type: ignore
    DISPLAY_ORDER: t.ClassVar[t.Literal[17]] = 17  # type: ignore
    driver: t.Literal["pyodbc", "mssql-python"] = "pyodbc"
    workspace_id: str
    tenant_id: str
    autocommit: t.Optional[bool] = True

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        from sqlmesh.core.engine_adapter.fabric import FabricEngineAdapter

        return FabricEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        # Override to support catalog switching for Fabric
        base_factory = super()._connection_factory

        def create_fabric_connection(
            target_catalog: t.Optional[str] = None, *args: t.Any, **kwargs: t.Any
        ) -> t.Callable:
            kwargs["database"] = target_catalog or self.database
            return base_factory(*args, **kwargs)

        return create_fabric_connection

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {
            "database": self.database,
            # more operations than not require a specific catalog to be already active
            # in particular, create/drop view, create/drop schema and querying information_schema
            "catalog_support": CatalogSupport.REQUIRES_SET_CATALOG,
            "workspace_id": self.workspace_id,
            "tenant_id": self.tenant_id,
            "user": self.user,
            "password": self.password,
        }


class SparkConnectionConfig(ConnectionConfig):
    """
    Vanilla Spark Connection Configuration. Use `DatabricksConnectionConfig` for Databricks.
    """

    config_dir: t.Optional[str] = None
    catalog: t.Optional[str] = None
    config: t.Dict[str, t.Any] = {}
    wap_enabled: bool = False

    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: t.Literal[False] = False

    type_: t.Literal["spark"] = Field(alias="type", default="spark")
    DIALECT: t.ClassVar[t.Literal["spark"]] = "spark"
    DISPLAY_NAME: t.ClassVar[t.Literal["Spark"]] = "Spark"
    DISPLAY_ORDER: t.ClassVar[t.Literal[8]] = 8

    _engine_import_validator = _get_engine_import_validator("pyspark", "spark")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "catalog",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.SparkEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from sqlmesh.engines.spark.db_api.spark_session import connection

        return connection

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        from pyspark.conf import SparkConf
        from pyspark.sql import SparkSession

        spark_config = SparkConf()
        if self.config:
            for k, v in self.config.items():
                spark_config.set(k, v)

        if self.config_dir:
            os.environ["SPARK_CONF_DIR"] = self.config_dir
        return {
            "spark": SparkSession.builder.config(conf=spark_config)
            .enableHiveSupport()
            .getOrCreate(),
        }

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {"wap_enabled": self.wap_enabled}


class TrinoAuthenticationMethod(str, Enum):
    NO_AUTH = "no-auth"
    BASIC = "basic"
    LDAP = "ldap"
    KERBEROS = "kerberos"
    JWT = "jwt"
    CERTIFICATE = "certificate"
    OAUTH = "oauth"

    @property
    def is_no_auth(self) -> bool:
        return self == self.NO_AUTH

    @property
    def is_basic(self) -> bool:
        return self == self.BASIC

    @property
    def is_ldap(self) -> bool:
        return self == self.LDAP

    @property
    def is_kerberos(self) -> bool:
        return self == self.KERBEROS

    @property
    def is_jwt(self) -> bool:
        return self == self.JWT

    @property
    def is_certificate(self) -> bool:
        return self == self.CERTIFICATE

    @property
    def is_oauth(self) -> bool:
        return self == self.OAUTH


class TrinoConnectionConfig(ConnectionConfig):
    method: TrinoAuthenticationMethod = TrinoAuthenticationMethod.NO_AUTH
    host: str
    user: str
    catalog: str
    port: t.Optional[int] = None
    http_scheme: t.Literal["http", "https"] = "https"
    # General Optional
    roles: t.Optional[t.Dict[str, str]] = None
    http_headers: t.Optional[t.Dict[str, str]] = None
    session_properties: t.Optional[t.Dict[str, str]] = None
    retries: int = 3
    timezone: t.Optional[str] = None
    # Basic/LDAP
    password: t.Optional[str] = None
    verify: t.Optional[bool] = None  # disable SSL verification (ignored if `cert` is provided)
    # LDAP
    impersonation_user: t.Optional[str] = None
    # Kerberos
    keytab: t.Optional[str] = None
    krb5_config: t.Optional[str] = None
    principal: t.Optional[str] = None
    service_name: str = "trino"
    hostname_override: t.Optional[str] = None
    mutual_authentication: bool = False
    force_preemptive: bool = False
    sanitize_mutual_error_response: bool = True
    delegate: bool = False
    # JWT
    jwt_token: t.Optional[str] = None
    # Certificate
    client_certificate: t.Optional[str] = None
    client_private_key: t.Optional[str] = None
    cert: t.Optional[str] = None
    source: str = "sqlmesh"

    # SQLMesh options
    schema_location_mapping: t.Optional[dict[re.Pattern, str]] = None
    timestamp_mapping: t.Optional[dict[exp.DataType, exp.DataType]] = None
    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: t.Literal[False] = False

    type_: t.Literal["trino"] = Field(alias="type", default="trino")
    DIALECT: t.ClassVar[t.Literal["trino"]] = "trino"
    DISPLAY_NAME: t.ClassVar[t.Literal["Trino"]] = "Trino"
    DISPLAY_ORDER: t.ClassVar[t.Literal[9]] = 9

    _engine_import_validator = _get_engine_import_validator("trino", "trino")

    @field_validator("schema_location_mapping", mode="before")
    @classmethod
    def _validate_regex_keys(
        cls, value: t.Dict[str | re.Pattern, str]
    ) -> t.Dict[re.Pattern, t.Any]:
        compiled = compile_regex_mapping(value)
        for replacement in compiled.values():
            if "@{schema_name}" not in replacement:
                raise ConfigError(
                    "schema_location_mapping needs to include the '@{schema_name}' placeholder in the value so SQLMesh knows where to substitute the schema name"
                )
        return compiled

    @field_validator("timestamp_mapping", mode="before")
    @classmethod
    def _validate_timestamp_mapping(
        cls, value: t.Optional[dict[str, str]]
    ) -> t.Optional[dict[exp.DataType, exp.DataType]]:
        if value is None:
            return value

        result: dict[exp.DataType, exp.DataType] = {}
        for source_type, target_type in value.items():
            try:
                source_datatype = exp.DataType.build(source_type)
            except ParseError:
                raise ConfigError(
                    f"Invalid SQL type string in timestamp_mapping: "
                    f"'{source_type}' is not a valid SQL data type."
                )
            try:
                target_datatype = exp.DataType.build(target_type)
            except ParseError:
                raise ConfigError(
                    f"Invalid SQL type string in timestamp_mapping: "
                    f"'{target_type}' is not a valid SQL data type."
                )
            result[source_datatype] = target_datatype

        return result

    @model_validator(mode="after")
    def _root_validator(self) -> Self:
        port = self.port
        if self.http_scheme == "http" and not self.method.is_no_auth and not self.method.is_basic:
            raise ConfigError("HTTP scheme can only be used with no-auth or basic method")

        if port is None:
            self.port = 80 if self.http_scheme == "http" else 443

        if (self.method.is_ldap or self.method.is_basic) and (not self.password or not self.user):
            raise ConfigError(
                f"Username and Password must be provided if using {self.method.value} authentication"
            )

        if self.method.is_kerberos and (
            not self.principal or not self.keytab or not self.krb5_config
        ):
            raise ConfigError(
                "Kerberos requires the following fields: principal, keytab, and krb5_config"
            )

        if self.method.is_jwt and not self.jwt_token:
            raise ConfigError("JWT requires `jwt_token` to be set")

        if self.method.is_certificate and (
            not self.cert or not self.client_certificate or not self.client_private_key
        ):
            raise ConfigError(
                "Certificate requires the following fields: cert, client_certificate, and client_private_key"
            )

        return self

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        kwargs = {
            "host",
            "port",
            "catalog",
            "roles",
            "source",
            "http_scheme",
            "http_headers",
            "session_properties",
            "timezone",
        }
        return kwargs

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.TrinoEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from trino.dbapi import connect

        return connect

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        from trino.auth import (
            BasicAuthentication,
            CertificateAuthentication,
            JWTAuthentication,
            KerberosAuthentication,
            OAuth2Authentication,
        )

        if self.method.is_basic or self.method.is_ldap:
            auth = BasicAuthentication(self.user, self.password)
        elif self.method.is_kerberos:
            if self.keytab:
                os.environ["KRB5_CLIENT_KTNAME"] = self.keytab
            auth = KerberosAuthentication(
                config=self.krb5_config,
                service_name=self.service_name,
                principal=self.principal,
                mutual_authentication=self.mutual_authentication,
                ca_bundle=self.cert,
                force_preemptive=self.force_preemptive,
                hostname_override=self.hostname_override,
                sanitize_mutual_error_response=self.sanitize_mutual_error_response,
                delegate=self.delegate,
            )
        elif self.method.is_oauth:
            auth = OAuth2Authentication()
        elif self.method.is_jwt:
            auth = JWTAuthentication(self.jwt_token)
        elif self.method.is_certificate:
            auth = CertificateAuthentication(self.client_certificate, self.client_private_key)
        else:
            auth = None

        return {
            "auth": auth,
            "user": self.impersonation_user or self.user,
            "max_attempts": self.retries,
            "verify": self.cert if self.cert is not None else self.verify,
            "source": self.source,
        }

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {
            "schema_location_mapping": self.schema_location_mapping,
            "timestamp_mapping": self.timestamp_mapping,
        }


class ClickhouseConnectionConfig(ConnectionConfig):
    """
    Clickhouse Connection Configuration.

    Property reference: https://clickhouse.com/docs/en/integrations/python#client-initialization
    """

    host: str
    username: str
    password: t.Optional[str] = None
    port: t.Optional[int] = None
    cluster: t.Optional[str] = None
    connect_timeout: int = 10
    send_receive_timeout: int = 300
    query_limit: int = 0
    use_compression: bool = True
    compression_method: t.Optional[str] = None
    connection_settings: t.Optional[t.Dict[str, t.Any]] = None
    http_proxy: t.Optional[str] = None
    # HTTPS/TLS settings
    verify: bool = True
    ca_cert: t.Optional[str] = None
    client_cert: t.Optional[str] = None
    client_cert_key: t.Optional[str] = None
    https_proxy: t.Optional[str] = None
    server_host_name: t.Optional[str] = None
    tls_mode: t.Optional[str] = None

    concurrent_tasks: int = 1
    register_comments: bool = True
    pre_ping: bool = False

    # This object expects options from urllib3 and also from clickhouse-connect
    # See:
    # * https://urllib3.readthedocs.io/en/stable/advanced-usage.html
    # * https://clickhouse.com/docs/en/integrations/python#customizing-the-http-connection-pool
    connection_pool_options: t.Optional[t.Dict[str, t.Any]] = None

    type_: t.Literal["clickhouse"] = Field(alias="type", default="clickhouse")
    DIALECT: t.ClassVar[t.Literal["clickhouse"]] = "clickhouse"
    DISPLAY_NAME: t.ClassVar[t.Literal["ClickHouse"]] = "ClickHouse"
    DISPLAY_ORDER: t.ClassVar[t.Literal[6]] = 6

    _engine_import_validator = _get_engine_import_validator("clickhouse_connect", "clickhouse")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        kwargs = {
            "host",
            "username",
            "port",
            "password",
            "connect_timeout",
            "send_receive_timeout",
            "query_limit",
            "http_proxy",
            "verify",
            "ca_cert",
            "client_cert",
            "client_cert_key",
            "https_proxy",
            "server_host_name",
            "tls_mode",
        }
        return kwargs

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.ClickhouseEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from functools import partial

        from clickhouse_connect.dbapi import connect  # type: ignore
        from clickhouse_connect.driver import httputil  # type: ignore

        pool_manager_options: t.Dict[str, t.Any] = dict(
            # Match the maxsize to the number of concurrent tasks
            maxsize=self.concurrent_tasks,
            # Block if there are no free connections
            block=True,
            verify=self.verify,
            ca_cert=self.ca_cert,
            client_cert=self.client_cert,
            client_cert_key=self.client_cert_key,
            https_proxy=self.https_proxy,
        )
        # this doesn't happen automatically because we always supply our own pool manager to the connection
        # https://github.com/ClickHouse/clickhouse-connect/blob/3a7f4b04cad29c7c2536661b831fb744248e2ec0/clickhouse_connect/driver/httpclient.py#L109
        if self.server_host_name:
            pool_manager_options["server_hostname"] = self.server_host_name
            if self.verify:
                pool_manager_options["assert_hostname"] = self.server_host_name
        if self.connection_pool_options:
            pool_manager_options.update(self.connection_pool_options)
        pool_mgr = httputil.get_pool_manager(**pool_manager_options)

        return partial(connect, pool_mgr=pool_mgr)

    @property
    def cloud_mode(self) -> bool:
        return "clickhouse.cloud" in self.host

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {"cluster": self.cluster, "cloud_mode": self.cloud_mode}

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        from sqlmesh import __version__

        # False = no compression
        # True = Clickhouse default compression method
        # string = specific compression method
        compress: bool | str = self.use_compression
        if compress and self.compression_method:
            compress = self.compression_method

        # Clickhouse system settings passed to connection
        # https://clickhouse.com/docs/en/operations/settings/settings
        # - below are set to align with dbt-clickhouse
        # - https://github.com/ClickHouse/dbt-clickhouse/blob/44d26308ea6a3c8ead25c280164aa88191f05f47/dbt/adapters/clickhouse/dbclient.py#L77
        settings = self.connection_settings or {}
        #  mutations_sync = 2: "The query waits for all mutations [ALTER statements] to complete on all replicas (if they exist)"
        settings["mutations_sync"] = "2"
        #  insert_distributed_sync = 1: "INSERT operation succeeds only after all the data is saved on all shards"
        settings["insert_distributed_sync"] = "1"
        if self.cluster or self.cloud_mode:
            # database_replicated_enforce_synchronous_settings = 1:
            #   - "Enforces synchronous waiting for some queries"
            #   - https://github.com/ClickHouse/ClickHouse/blob/ccaa8d03a9351efc16625340268b9caffa8a22ba/src/Core/Settings.h#L709
            settings["database_replicated_enforce_synchronous_settings"] = "1"
            # insert_quorum = auto:
            #   - "INSERT succeeds only when ClickHouse manages to correctly write data to the insert_quorum of replicas during
            #       the insert_quorum_timeout"
            #   - "use majority number (number_of_replicas / 2 + 1) as quorum number"
            settings["insert_quorum"] = "auto"

        return {
            "compress": compress,
            "client_name": f"SQLMesh/{__version__}",
            **settings,
        }


class AthenaConnectionConfig(ConnectionConfig):
    # PyAthena connection options
    aws_access_key_id: t.Optional[str] = None
    aws_secret_access_key: t.Optional[str] = None
    role_arn: t.Optional[str] = None
    role_session_name: t.Optional[str] = None
    region_name: t.Optional[str] = None
    work_group: t.Optional[str] = None
    s3_staging_dir: t.Optional[str] = None
    schema_name: t.Optional[str] = None
    catalog_name: t.Optional[str] = None

    # SQLMesh options
    s3_warehouse_location: t.Optional[str] = None
    concurrent_tasks: int = 4
    register_comments: t.Literal[False] = (
        False  # because Athena doesnt support comments in most cases
    )
    pre_ping: t.Literal[False] = False

    type_: t.Literal["athena"] = Field(alias="type", default="athena")
    DIALECT: t.ClassVar[t.Literal["athena"]] = "athena"
    DISPLAY_NAME: t.ClassVar[t.Literal["Athena"]] = "Athena"
    DISPLAY_ORDER: t.ClassVar[t.Literal[15]] = 15

    _engine_import_validator = _get_engine_import_validator("pyathena", "athena")

    @model_validator(mode="after")
    def _root_validator(self) -> Self:
        work_group = self.work_group
        s3_staging_dir = self.s3_staging_dir
        s3_warehouse_location = self.s3_warehouse_location

        if not work_group and not s3_staging_dir:
            raise ConfigError("At least one of work_group or s3_staging_dir must be set")

        if s3_staging_dir:
            self.s3_staging_dir = validate_s3_uri(s3_staging_dir, base=True, error_type=ConfigError)

        if s3_warehouse_location:
            self.s3_warehouse_location = validate_s3_uri(
                s3_warehouse_location, base=True, error_type=ConfigError
            )

        return self

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "aws_access_key_id",
            "aws_secret_access_key",
            "role_arn",
            "role_session_name",
            "region_name",
            "work_group",
            "s3_staging_dir",
            "schema_name",
            "catalog_name",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.AthenaEngineAdapter

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {"s3_warehouse_location": self.s3_warehouse_location}

    @property
    def _connection_factory(self) -> t.Callable:
        from pyathena import connect  # type: ignore

        return connect

    def get_catalog(self) -> t.Optional[str]:
        return self.catalog_name


class RisingwaveConnectionConfig(ConnectionConfig):
    host: str
    user: str
    password: t.Optional[str] = None
    port: int
    database: str
    role: t.Optional[str] = None
    sslmode: t.Optional[str] = None

    concurrent_tasks: int = 4
    register_comments: bool = True
    pre_ping: bool = True

    type_: t.Literal["risingwave"] = Field(alias="type", default="risingwave")
    DIALECT: t.ClassVar[t.Literal["risingwave"]] = "risingwave"
    DISPLAY_NAME: t.ClassVar[t.Literal["RisingWave"]] = "RisingWave"
    DISPLAY_ORDER: t.ClassVar[t.Literal[16]] = 16

    _engine_import_validator = _get_engine_import_validator("psycopg2", "risingwave")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "host",
            "user",
            "password",
            "port",
            "database",
            "role",
            "sslmode",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.RisingwaveEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from psycopg2 import connect

        return connect

    @property
    def _cursor_init(self) -> t.Optional[t.Callable[[t.Any], None]]:
        def init(cursor: t.Any) -> None:
            sql = "SET RW_IMPLICIT_FLUSH TO true;"
            cursor.execute(sql)

        return init


CONNECTION_CONFIG_TO_TYPE = {
    # Map all subclasses of ConnectionConfig to the value of their `type_` field.
    tpe.all_field_infos()["type_"].default: tpe
    for tpe in subclasses(
        __name__,
        ConnectionConfig,
        exclude=(ConnectionConfig, BaseDuckDBConnectionConfig),
    )
}

DIALECT_TO_TYPE = {
    tpe.all_field_infos()["type_"].default: tpe.DIALECT
    for tpe in subclasses(
        __name__,
        ConnectionConfig,
        exclude=(ConnectionConfig, BaseDuckDBConnectionConfig),
    )
}

INIT_DISPLAY_INFO_TO_TYPE = {
    tpe.all_field_infos()["type_"].default: (
        tpe.DISPLAY_ORDER,
        tpe.DISPLAY_NAME,
    )
    for tpe in subclasses(
        __name__,
        ConnectionConfig,
        exclude=(ConnectionConfig, BaseDuckDBConnectionConfig),
    )
}


def parse_connection_config(v: t.Dict[str, t.Any]) -> ConnectionConfig:
    if "type" not in v:
        raise ConfigError("Missing connection type.")

    connection_type = v["type"]
    if connection_type not in CONNECTION_CONFIG_TO_TYPE:
        raise ConfigError(f"Unknown connection type '{connection_type}'.")

    return CONNECTION_CONFIG_TO_TYPE[connection_type](**v)


def _connection_config_validator(
    cls: t.Type, v: ConnectionConfig | t.Dict[str, t.Any] | None
) -> ConnectionConfig | None:
    if v is None or isinstance(v, ConnectionConfig):
        return v

    check_config_and_vars_msg = "\n\nVerify your config.yaml and environment variables."

    try:
        return parse_connection_config(v)
    except pydantic.ValidationError as e:
        raise ConfigError(
            validation_error_message(e, f"Invalid '{v['type']}' connection config:")
            + check_config_and_vars_msg
        )
    except ConfigError as e:
        raise ConfigError(str(e) + check_config_and_vars_msg)


connection_config_validator: t.Callable = field_validator(
    "connection",
    "state_connection",
    "test_connection",
    "default_connection",
    "default_test_connection",
    mode="before",
    check_fields=False,
)(_connection_config_validator)


if t.TYPE_CHECKING:
    # TypeAlias hasn't been introduced until Python 3.10 which means that we can't use it
    # outside the TYPE_CHECKING guard.
    SerializableConnectionConfig: t.TypeAlias = ConnectionConfig  # type: ignore
else:
    import pydantic

    # Workaround for https://docs.pydantic.dev/latest/concepts/serialization/#serializing-with-duck-typing
    SerializableConnectionConfig = pydantic.SerializeAsAny[ConnectionConfig]  # type: ignore
