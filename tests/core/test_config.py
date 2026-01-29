import os
import pathlib
import re
from pathlib import Path
from unittest import mock
import typing as t

import pytest
from pytest_mock import MockerFixture
from sqlglot import exp

from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
    BigQueryConnectionConfig,
    MotherDuckConnectionConfig,
    BuiltInSchedulerConfig,
    EnvironmentSuffixTarget,
    TableNamingConvention,
)
from sqlmesh.core.config.connection import DuckDBAttachOptions, RedshiftConnectionConfig
from sqlmesh.core.config.loader import (
    load_config_from_env,
    load_config_from_paths,
    load_config_from_python_module,
    load_configs,
)
from sqlmesh.core.context import Context
from sqlmesh.core.engine_adapter.athena import AthenaEngineAdapter
from sqlmesh.core.engine_adapter.duckdb import DuckDBEngineAdapter
from sqlmesh.core.engine_adapter.redshift import RedshiftEngineAdapter
from sqlmesh.core.notification_target import ConsoleNotificationTarget
from sqlmesh.core.user import User
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils import yaml
from sqlmesh.dbt.loader import DbtLoader
from tests.utils.test_filesystem import create_temp_file


@pytest.fixture(scope="session")
def yaml_config_path(tmp_path_factory) -> Path:
    config_path = tmp_path_factory.mktemp("yaml_config") / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
gateways:
    another_gateway:
        connection:
            type: duckdb
            database: test_db

model_defaults:
    dialect: ''
        """
        )
    return config_path


@pytest.fixture(scope="session")
def python_config_path(tmp_path_factory) -> Path:
    config_path = tmp_path_factory.mktemp("python_config") / "config.py"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig, ModelDefaultsConfig
config = Config(gateways=GatewayConfig(connection=DuckDBConnectionConfig()), model_defaults=ModelDefaultsConfig(dialect=''))
        """
        )
    return config_path


def test_update_with_gateways():
    gateway0_config = GatewayConfig(connection=DuckDBConnectionConfig())
    gateway1_config = GatewayConfig(connection=DuckDBConnectionConfig(database="test"))

    assert Config(gateways=gateway0_config).update_with(
        Config(gateways={"gateway1": gateway1_config})
    ) == Config(gateways={"": gateway0_config, "gateway1": gateway1_config})

    assert Config(gateways={"gateway1": gateway1_config}).update_with(
        Config(gateways=gateway0_config)
    ) == Config(gateways={"gateway1": gateway1_config, "": gateway0_config})

    assert Config(gateways=gateway0_config).update_with(Config(gateways=gateway1_config)) == Config(
        gateways=gateway1_config
    )

    assert Config(gateways={"gateway0": gateway0_config}).update_with(
        Config(gateways={"gateway1": gateway1_config})
    ) == Config(gateways={"gateway0": gateway0_config, "gateway1": gateway1_config})


def test_update_with_users():
    user_a = User(username="a")
    user_b = User(username="b")

    assert Config(users=[user_a]).update_with(Config(users=[user_b])) == Config(
        users=[user_a, user_b]
    )


def test_update_with_ignore_patterns():
    pattern_a = "pattern_a"
    pattern_b = "pattern_b"

    assert Config(ignore_patterns=[pattern_a]).update_with(
        Config(ignore_patterns=[pattern_b])
    ) == Config(ignore_patterns=[pattern_a, pattern_b])


def test_update_with_notification_targets():
    target = ConsoleNotificationTarget()

    assert Config(notification_targets=[target]).update_with(
        Config(notification_targets=[target])
    ) == Config(notification_targets=[target] * 2)


def test_update_with_model_defaults():
    config_a = Config(model_defaults=ModelDefaultsConfig(start="2022-01-01", dialect="duckdb"))
    config_b = Config(model_defaults=ModelDefaultsConfig(dialect="spark"))

    assert config_a.update_with(config_b) == Config(
        model_defaults=ModelDefaultsConfig(start="2022-01-01", dialect="spark")
    )


def test_default_gateway():
    gateway_a = GatewayConfig(connection=DuckDBConnectionConfig(database="test_db_a"))
    gateway_b = GatewayConfig(connection=DuckDBConnectionConfig(database="test_db_b"))
    gateway_c = GatewayConfig(connection=DuckDBConnectionConfig(database="test_db_c"))

    config = Config(
        gateways={
            "gateway1": gateway_b,
            "gateway2": gateway_c,
            "": gateway_a,
        },
    )

    assert config.get_gateway() == gateway_a

    assert config.copy(update={"default_gateway": "gateway2"}).get_gateway() == gateway_c

    assert (
        Config(
            gateways={
                "gateway1": gateway_b,
                "gateway2": gateway_c,
            },
        ).get_gateway()
        == gateway_b
    )

    with pytest.raises(
        ConfigError,
        match="Missing gateway with name 'missing'",
    ):
        config.copy(update={"default_gateway": "missing"}).get_gateway()


def test_load_config_from_paths(yaml_config_path: Path, python_config_path: Path):
    config: Config = load_config_from_paths(
        Config,
        project_paths=[yaml_config_path, python_config_path],
    )

    assert config == Config(
        gateways={  # type: ignore
            "another_gateway": GatewayConfig(connection=DuckDBConnectionConfig(database="test_db")),
            "": GatewayConfig(connection=DuckDBConnectionConfig()),
        },
        model_defaults=ModelDefaultsConfig(dialect=""),
    )


def test_load_config_multiple_config_files_in_folder(tmp_path):
    config_a_path = tmp_path / "config.yaml"
    with open(config_a_path, "w", encoding="utf-8") as fd:
        fd.write("project: project_a")

    config_b_path = tmp_path / "config.yml"
    with open(config_b_path, "w", encoding="utf-8") as fd:
        fd.write("project: project_b")

    with pytest.raises(ConfigError, match=r"^Multiple configuration files found in folder.*"):
        load_config_from_paths(Config, project_paths=[config_a_path, config_b_path])


def test_load_config_no_config():
    with pytest.raises(ConfigError, match=r"^SQLMesh project config could not be found.*"):
        load_config_from_paths(Config, load_from_env=False)


def test_load_config_no_dialect(tmp_path):
    create_temp_file(
        tmp_path,
        pathlib.Path("config.yaml"),
        """
gateways:
    local:
        connection:
            type: duckdb
            database: db.db
""",
    )

    create_temp_file(
        tmp_path,
        pathlib.Path("config.py"),
        """
from sqlmesh.core.config import Config, DuckDBConnectionConfig

config = Config(default_connection=DuckDBConnectionConfig())
""",
    )

    with pytest.raises(
        ConfigError, match=r"^Default model SQL dialect is a required configuration parameter.*"
    ):
        load_config_from_paths(Config, project_paths=[tmp_path / "config.yaml"])

    with pytest.raises(
        ConfigError, match=r"^Default model SQL dialect is a required configuration parameter.*"
    ):
        load_config_from_paths(Config, project_paths=[tmp_path / "config.py"])


def test_load_config_bad_model_default_key(tmp_path):
    create_temp_file(
        tmp_path,
        pathlib.Path("config.yaml"),
        """
gateways:
    local:
        connection:
            type: duckdb
            database: db.db

model_defaults:
    dialect: ''
    test: 1
""",
    )

    with pytest.raises(
        ConfigError, match=r"^'test' is not a valid model default configuration key.*"
    ):
        load_config_from_paths(Config, project_paths=[tmp_path / "config.yaml"])


def test_load_config_unsupported_extension(tmp_path):
    config_path = tmp_path / "config.txt"
    config_path.touch()

    with pytest.raises(ConfigError, match=r"^Unsupported config file extension 'txt'.*"):
        load_config_from_paths(Config, project_paths=[config_path])


def test_load_python_config_with_personal_config(tmp_path):
    create_temp_file(
        tmp_path / "personal",
        pathlib.Path("config.yaml"),
        """
gateways:
    local:
        connection:
            type: duckdb
            database: db.db
""",
    )

    create_temp_file(
        tmp_path,
        pathlib.Path("config.py"),
        """
from sqlmesh.core.config import Config, DuckDBConnectionConfig, ModelDefaultsConfig

custom_config = Config(default_connection=DuckDBConnectionConfig(), model_defaults=ModelDefaultsConfig(dialect="duckdb"))
""",
    )
    config = load_config_from_paths(
        Config,
        project_paths=[tmp_path / "config.py"],
        personal_paths=[tmp_path / "personal" / "config.yaml"],
        config_name="custom_config",
    )
    assert config.gateways["local"].connection.database == "db.db"
    assert config.default_connection.database is None
    assert config.model_defaults.dialect == "duckdb"


def test_load_config_from_env():
    with mock.patch.dict(
        os.environ,
        {
            "SQLMESH__GATEWAY__CONNECTION__TYPE": "duckdb",
            "SQLMESH__GATEWAY__CONNECTION__DATABASE": "test_db",
        },
    ):
        assert Config.parse_obj(load_config_from_env()) == Config(
            gateways=GatewayConfig(connection=DuckDBConnectionConfig(database="test_db")),
        )


def test_load_config_from_env_fails():
    with mock.patch.dict(os.environ, {"SQLMESH__GATEWAYS__ABCDEF__CONNECTION__PASSWORD": "..."}):
        with pytest.raises(
            ConfigError,
            match="Missing connection type.\n\nVerify your config.yaml and environment variables.",
        ):
            Config.parse_obj(load_config_from_env())


def test_load_config_from_env_no_config_vars():
    with mock.patch.dict(
        os.environ,
        {
            "DUMMY_ENV_VAR": "dummy",
        },
    ):
        assert load_config_from_env() == {}


def test_load_config_from_env_invalid_variable_name():
    with mock.patch.dict(
        os.environ,
        {
            "SQLMESH__": "",
        },
    ):
        with pytest.raises(
            ConfigError,
            match="Invalid SQLMesh configuration variable 'sqlmesh__'.",
        ):
            load_config_from_env()


def test_load_yaml_config_env_var_gateway_override(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("yaml_config") / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
gateways:
    testing:
        connection:
            type: motherduck
            database: blah
model_defaults:
    dialect: bigquery
        """
        )
    with mock.patch.dict(
        os.environ,
        {
            "SQLMESH__GATEWAYS__TESTING__STATE_CONNECTION__TYPE": "bigquery",
            "SQLMESH__GATEWAYS__TESTING__STATE_CONNECTION__CHECK_IMPORT": "false",
            "SQLMESH__DEFAULT_GATEWAY": "testing",
        },
    ):
        assert load_config_from_paths(
            Config,
            project_paths=[config_path],
        ) == Config(
            gateways={
                "testing": GatewayConfig(
                    connection=MotherDuckConnectionConfig(database="blah"),
                    state_connection=BigQueryConnectionConfig(check_import=False),
                ),
            },
            model_defaults=ModelDefaultsConfig(dialect="bigquery"),
            default_gateway="testing",
        )


def test_load_py_config_env_var_gateway_override(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("python_config") / "config.py"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig, ModelDefaultsConfig
config = Config(gateways={"duckdb_gateway": GatewayConfig(connection=DuckDBConnectionConfig())}, model_defaults=ModelDefaultsConfig(dialect=''))
        """
        )
    with mock.patch.dict(
        os.environ,
        {
            "SQLMESH__GATEWAYS__DUCKDB_GATEWAY__STATE_CONNECTION__TYPE": "bigquery",
            "SQLMESH__GATEWAYS__DUCKDB_GATEWAY__STATE_CONNECTION__CHECK_IMPORT": "false",
            "SQLMESH__DEFAULT_GATEWAY": "duckdb_gateway",
        },
    ):
        config = load_config_from_paths(
            Config,
            project_paths=[config_path],
        )
        assert config == Config(
            gateways={  # type: ignore
                "duckdb_gateway": GatewayConfig(
                    connection=DuckDBConnectionConfig(),
                    state_connection=BigQueryConnectionConfig(check_import=False),
                ),
            },
            model_defaults=ModelDefaultsConfig(dialect=""),
            default_gateway="duckdb_gateway",
        )


def test_load_config_from_python_module_missing_config(tmp_path):
    config_path = tmp_path / "missing_config.py"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write("from sqlmesh.core.config import Config")

    with pytest.raises(ConfigError, match="Config 'config' was not found."):
        load_config_from_python_module(Config, config_path)


def test_load_config_from_python_module_invalid_config_object(tmp_path):
    config_path = tmp_path / "invalid_config.py"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write("config = None")

    with pytest.raises(
        ConfigError,
        match=r"^Config needs to be a valid object.*",
    ):
        load_config_from_python_module(Config, config_path)


@pytest.mark.parametrize(
    [
        "mapping",
        "expected",
        "dialect",
        "raise_error",
    ],
    [
        (
            "'^dev$': dev_catalog\n    '^other$': other_catalog",
            {re.compile("^dev$"): "dev_catalog", re.compile("^other$"): "other_catalog"},
            "duckdb",
            "",
        ),
        (
            "'^(?!prod$)': dev",
            {re.compile("^(?!prod$)"): "dev"},
            "duckdb",
            "",
        ),
        (
            "'^dev$': dev_catalog\n    '[': other_catalog",
            {},
            "duckdb",
            "`\\[` is not a valid regular expression.",
        ),
        (
            "'^prod$': prod_catalog",
            {re.compile("^prod$"): "PROD_CATALOG"},
            "snowflake",
            "",
        ),
    ],
)
def test_environment_catalog_mapping(tmp_path_factory, mapping, expected, dialect, raise_error):
    config_path = tmp_path_factory.mktemp("yaml_config") / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            f"""
gateways:
    local:
        connection:
            type: duckdb

model_defaults:
    dialect: {dialect}

environment_catalog_mapping:
    {mapping}
        """
        )
    if raise_error:
        with pytest.raises(ConfigError, match=raise_error):
            load_config_from_paths(
                Config,
                project_paths=[config_path],
            )
    else:
        assert (
            load_config_from_paths(Config, project_paths=[config_path]).environment_catalog_mapping
            == expected
        )


def test_physical_schema_mapping_mutually_exclusive_with_physical_schema_override() -> None:
    Config(physical_schema_override={"foo": "bar"})  # type: ignore
    Config(physical_schema_mapping={"^foo$": "bar"})

    with pytest.raises(
        ConfigError, match=r"Only one.*physical_schema_override.*physical_schema_mapping"
    ):
        Config(physical_schema_override={"foo": "bar"}, physical_schema_mapping={"^foo$": "bar"})  # type: ignore


def test_load_alternative_config_type(yaml_config_path: Path, python_config_path: Path):
    class DerivedConfig(Config):
        pass

    config = load_config_from_paths(
        DerivedConfig,
        project_paths=[yaml_config_path, python_config_path],
    )

    assert config == DerivedConfig(
        gateways={  # type: ignore
            "another_gateway": GatewayConfig(connection=DuckDBConnectionConfig(database="test_db")),
            "": GatewayConfig(connection=DuckDBConnectionConfig()),
        },
        model_defaults=ModelDefaultsConfig(dialect=""),
    )


def test_connection_config_serialization():
    config = Config(
        default_connection=DuckDBConnectionConfig(database="my_db"),
        default_test_connection=DuckDBConnectionConfig(database="my_test_db"),
    )
    serialized = config.dict()
    assert serialized["default_connection"] == {
        "concurrent_tasks": 1,
        "register_comments": True,
        "type": "duckdb",
        "extensions": [],
        "pre_ping": False,
        "pretty_sql": False,
        "connector_config": {},
        "secrets": [],
        "filesystems": [],
        "database": "my_db",
    }
    assert serialized["default_test_connection"] == {
        "concurrent_tasks": 1,
        "register_comments": True,
        "type": "duckdb",
        "extensions": [],
        "pre_ping": False,
        "pretty_sql": False,
        "connector_config": {},
        "secrets": [],
        "filesystems": [],
        "database": "my_test_db",
    }


def test_variables():
    variables = {
        "int_var": 1,
        "str_var": "test_value",
        "bool_var": True,
        "float_var": 1.0,
        "list_var": [1, 2, 3],
        "dict_var": {"a": "test_value", "b": 2},
    }
    gateway_variables = {
        "UPPERCASE_VAR": 2,
    }
    config = Config(
        variables=variables, gateways={"local": GatewayConfig(variables=gateway_variables)}
    )
    assert config.variables == variables
    assert config.get_gateway("local").variables == {"uppercase_var": 2}

    with pytest.raises(
        ConfigError, match="Unsupported variable value type: <class 'sqlglot.expressions.Column'>"
    ):
        Config(variables={"invalid_var": exp.column("sqlglot_expr")})


def test_load_duckdb_attach_config(tmp_path):
    config_path = tmp_path / "config_duckdb_attach.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
gateways:
    another_gateway:
        connection:
            type: duckdb
            catalogs:
              memory: ':memory:'
              sqlite:
                type: 'sqlite'
                path: 'test.db'
              postgres:
                type: 'postgres'
                path: 'dbname=postgres user=postgres host=127.0.0.1'
                read_only: true
model_defaults:
    dialect: ''
        """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    assert config.gateways["another_gateway"].connection.catalogs.get("memory") == ":memory:"

    attach_config_1 = config.gateways["another_gateway"].connection.catalogs.get("sqlite")

    assert isinstance(attach_config_1, DuckDBAttachOptions)
    assert attach_config_1.type == "sqlite"
    assert attach_config_1.path == "test.db"
    assert attach_config_1.read_only is False

    attach_config_2 = config.gateways["another_gateway"].connection.catalogs.get("postgres")

    assert isinstance(attach_config_2, DuckDBAttachOptions)
    assert attach_config_2.type == "postgres"
    assert attach_config_2.path == "dbname=postgres user=postgres host=127.0.0.1"
    assert attach_config_2.read_only is True


def test_load_model_defaults_audits(tmp_path):
    config_path = tmp_path / "config_model_defaults_audits.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
model_defaults:
    dialect: ''
    audits:
        - assert_positive_order_ids
        - does_not_exceed_threshold(column := id, threshold := 1000)
        """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    assert len(config.model_defaults.audits) == 2
    assert config.model_defaults.audits[0] == ("assert_positive_order_ids", {})
    assert config.model_defaults.audits[1][0] == "does_not_exceed_threshold"
    assert type(config.model_defaults.audits[1][1]["column"]) == exp.Column
    assert config.model_defaults.audits[1][1]["column"].this.this == "id"
    assert type(config.model_defaults.audits[1][1]["threshold"]) == exp.Literal
    assert config.model_defaults.audits[1][1]["threshold"].this == "1000"


def test_load_model_defaults_statements(tmp_path):
    config_path = tmp_path / "config_model_defaults_statements.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
model_defaults:
    dialect: duckdb
    pre_statements:
        - SET memory_limit = '10GB'
        - CREATE TEMP TABLE temp_data AS SELECT 1 as id
    post_statements:
        - DROP TABLE IF EXISTS temp_data
        - ANALYZE @this_model
        - SET memory_limit = '5GB'
    on_virtual_update:
        - UPDATE stats_table SET last_update = CURRENT_TIMESTAMP
        """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    assert config.model_defaults.pre_statements is not None
    assert len(config.model_defaults.pre_statements) == 2
    assert isinstance(exp.maybe_parse(config.model_defaults.pre_statements[0]), exp.Set)
    assert isinstance(exp.maybe_parse(config.model_defaults.pre_statements[1]), exp.Create)

    assert config.model_defaults.post_statements is not None
    assert len(config.model_defaults.post_statements) == 3
    assert isinstance(exp.maybe_parse(config.model_defaults.post_statements[0]), exp.Drop)
    assert isinstance(exp.maybe_parse(config.model_defaults.post_statements[1]), exp.Analyze)
    assert isinstance(exp.maybe_parse(config.model_defaults.post_statements[2]), exp.Set)

    assert config.model_defaults.on_virtual_update is not None
    assert len(config.model_defaults.on_virtual_update) == 1
    assert isinstance(exp.maybe_parse(config.model_defaults.on_virtual_update[0]), exp.Update)


def test_load_model_defaults_validation_statements(tmp_path):
    config_path = tmp_path / "config_model_defaults_statements_wrong.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
model_defaults:
    dialect: duckdb
    pre_statements:
        - 313
        """
        )

    with pytest.raises(TypeError, match=r"expected str instance, int found"):
        config = load_config_from_paths(
            Config,
            project_paths=[config_path],
        )


def test_scheduler_config(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("yaml_config") / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
gateways:
    builtin_gateway:
        scheduler:
            type: builtin

default_scheduler:
    type: builtin

model_defaults:
    dialect: bigquery
        """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )
    assert isinstance(config.default_scheduler, BuiltInSchedulerConfig)
    assert isinstance(config.get_gateway("builtin_gateway").scheduler, BuiltInSchedulerConfig)


def test_multi_gateway_config(tmp_path, mocker: MockerFixture):
    config_path = tmp_path / "config_athena_redshift.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
gateways:
    redshift:
        connection:
            type: redshift
            user: user
            password: '1234'
            host: host
            database: db
        test_connection:
            type: redshift
            database: test_db
        state_connection:
            type: duckdb
            database: state.db
    athena:
        connection:
            type: athena
            aws_access_key_id: '1234'
            aws_secret_access_key: accesskey
            work_group: group
            s3_warehouse_location: s3://location
    duckdb:
        connection:
            type: duckdb
            database: db.db

default_gateway: redshift

model_defaults:
    dialect: redshift
        """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    ctx = Context(paths=tmp_path, config=config)

    assert isinstance(ctx.connection_config, RedshiftConnectionConfig)
    assert len(ctx.engine_adapters) == 3
    assert isinstance(ctx.engine_adapters["athena"], AthenaEngineAdapter)
    assert isinstance(ctx.engine_adapters["redshift"], RedshiftEngineAdapter)
    assert isinstance(ctx.engine_adapters["duckdb"], DuckDBEngineAdapter)
    assert ctx.engine_adapter == ctx._get_engine_adapter("redshift")

    # The duckdb engine adapter should be have been set as multithreaded as well
    assert ctx.engine_adapters["duckdb"]._multithreaded


def test_multi_gateway_single_threaded_config(tmp_path):
    config_path = tmp_path / "config_duck_athena.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
gateways:
    duckdb:
        connection:
            type: duckdb
            database: db.db
    athena:
        connection:
            type: athena
            aws_access_key_id: '1234'
            aws_secret_access_key: accesskey
            work_group: group
            s3_warehouse_location: s3://location
default_gateway: duckdb
model_defaults:
    dialect: duckdb
        """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    ctx = Context(paths=tmp_path, config=config)
    assert isinstance(ctx.connection_config, DuckDBConnectionConfig)
    assert len(ctx.engine_adapters) == 2
    assert ctx.engine_adapter == ctx._get_engine_adapter("duckdb")
    assert isinstance(ctx.engine_adapters["athena"], AthenaEngineAdapter)

    # In this case athena should use 1 concurrent task as the default gateway is duckdb
    assert not ctx.engine_adapters["athena"]._multithreaded


def test_trino_schema_location_mapping_syntax(tmp_path):
    config_path = tmp_path / "config_trino.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
    gateways:
      trino:
        connection:
          type: trino
          user: trino
          host: trino
          catalog: trino
          schema_location_mapping:
            '^utils$': 's3://utils-bucket/@{schema_name}'
            '^landing\\..*$': 's3://raw-data/@{catalog_name}/@{schema_name}'

    default_gateway: trino

    model_defaults:
      dialect: trino
    """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    from sqlmesh.core.config.connection import TrinoConnectionConfig

    conn = config.gateways["trino"].connection
    assert isinstance(conn, TrinoConnectionConfig)

    assert len(conn.schema_location_mapping) == 2


def test_trino_source_option(tmp_path):
    config_path = tmp_path / "config_trino_source.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
    gateways:
      trino:
        connection:
          type: trino
          user: trino
          host: trino
          catalog: trino
          source: my_sqlmesh_source

    default_gateway: trino

    model_defaults:
      dialect: trino
    """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    from sqlmesh.core.config.connection import TrinoConnectionConfig

    conn = config.gateways["trino"].connection
    assert isinstance(conn, TrinoConnectionConfig)
    assert conn.source == "my_sqlmesh_source"


def test_gcp_postgres_ip_and_scopes(tmp_path):
    config_path = tmp_path / "config_gcp_postgres.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
    gateways:
      gcp_postgres:
        connection:
          type: gcp_postgres
          check_import: false
          instance_connection_string: something
          user: user
          password: password
          db: db
          ip_type: private
          scopes:
          - https://www.googleapis.com/auth/cloud-platform
          - https://www.googleapis.com/auth/sqlservice.admin

    default_gateway: gcp_postgres

    model_defaults:
      dialect: postgres
    """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    from sqlmesh.core.config.connection import GCPPostgresConnectionConfig

    conn = config.gateways["gcp_postgres"].connection
    assert isinstance(conn, GCPPostgresConnectionConfig)

    assert len(conn.scopes) == 2
    assert conn.scopes[0] == "https://www.googleapis.com/auth/cloud-platform"
    assert conn.scopes[1] == "https://www.googleapis.com/auth/sqlservice.admin"
    assert conn.ip_type == "private"


def test_gateway_model_defaults(tmp_path):
    global_defaults = ModelDefaultsConfig(
        dialect="snowflake", owner="foo", optimize_query=True, enabled=True, cron="@daily"
    )
    gateway_defaults = ModelDefaultsConfig(dialect="duckdb", owner="baz", optimize_query=False)

    config = Config(
        gateways={
            "duckdb": GatewayConfig(
                connection=DuckDBConnectionConfig(database="db.db"),
                model_defaults=gateway_defaults,
            )
        },
        model_defaults=global_defaults,
        default_gateway="duckdb",
    )

    ctx = Context(paths=tmp_path, config=config, gateway="duckdb")

    expected = ModelDefaultsConfig(
        dialect="duckdb", owner="baz", optimize_query=False, enabled=True, cron="@daily"
    )

    assert ctx.config.model_defaults == expected


def test_redshift_merge_flag(tmp_path, mocker: MockerFixture):
    config_path = tmp_path / "config_redshift_merge.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
gateways:
    redshift:
        connection:
            type: redshift
            user: user
            password: '1234'
            host: host
            database: db
            enable_merge: true
    default:
        connection:
            type: redshift
            user: user
            password: '1234'
            host: host
            database: db

default_gateway: redshift

model_defaults:
    dialect: redshift
        """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )
    redshift_connection = config.get_connection("redshift")
    assert isinstance(redshift_connection, RedshiftConnectionConfig)
    assert redshift_connection.enable_merge
    adapter = redshift_connection.create_engine_adapter()
    assert isinstance(adapter, RedshiftEngineAdapter)
    assert adapter.enable_merge

    adapter_2 = config.get_connection("default").create_engine_adapter()
    assert isinstance(adapter_2, RedshiftEngineAdapter)
    assert not adapter_2.enable_merge


def test_environment_statements_config(tmp_path):
    config_path = tmp_path / "config_before_after_all.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """
    gateways:
      postgres:
        connection:
          type: postgres
          database: db
          user: postgres
          password: postgres
          host: localhost
          port: 5432

    default_gateway: postgres

    before_all:
    - CREATE TABLE IF NOT EXISTS custom_analytics (physical_table VARCHAR, evaluation_time VARCHAR);
    after_all:
    - "@grant_schema_privileges()"
    - "GRANT REFERENCES ON FUTURE VIEWS IN DATABASE db TO ROLE admin_role;"

    model_defaults:
      dialect: postgres
    """
        )

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    assert config.before_all == [
        "CREATE TABLE IF NOT EXISTS custom_analytics (physical_table VARCHAR, evaluation_time VARCHAR);"
    ]
    assert config.after_all == [
        "@grant_schema_privileges()",
        "GRANT REFERENCES ON FUTURE VIEWS IN DATABASE db TO ROLE admin_role;",
    ]


# https://github.com/TobikoData/sqlmesh/pull/4049
def test_pydantic_import_error() -> None:
    class TestConfig(DuckDBConnectionConfig):
        pass

    TestConfig()


def test_config_subclassing() -> None:
    class ConfigSubclass(Config): ...

    ConfigSubclass()


def test_config_complex_types_supplied_as_json_strings_from_env(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
    gateways:
      bigquery:
        connection:
          type: bigquery
          project: unit-test

    default_gateway: bigquery

    model_defaults:
      dialect: bigquery
""")
    with mock.patch.dict(
        os.environ,
        {
            "SQLMESH__GATEWAYS__BIGQUERY__CONNECTION__SCOPES": '     ["a","b","c"]',  # note: leading whitespace is deliberate
            "SQLMESH__GATEWAYS__BIGQUERY__CONNECTION__KEYFILE_JSON": '{ "foo": "bar" }',
        },
    ):
        config = load_config_from_paths(
            Config,
            project_paths=[config_path],
        )

        conn = config.gateways["bigquery"].connection
        assert isinstance(conn, BigQueryConnectionConfig)

        assert conn.project == "unit-test"
        assert conn.scopes == ("a", "b", "c")
        assert conn.keyfile_json == {"foo": "bar"}


def test_config_user_macro_function(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
    gateways:
      bigquery:
        connection:
          type: bigquery
          project: unit-test

    default_gateway: bigquery

    model_defaults:
      dialect: bigquery

    default_target_environment: dev_{{ user() }}
""")

    with mock.patch("getpass.getuser", return_value="test_user"):
        config = load_config_from_paths(
            Config,
            project_paths=[config_path],
        )

    assert config.default_target_environment == "dev_test_user"


def test_environment_suffix_target_catalog(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
    gateways:
      warehouse:
        connection:
          type: duckdb

    default_gateway: warehouse

    model_defaults:
      dialect: duckdb

    environment_suffix_target: catalog
""")

    config = load_config_from_paths(
        Config,
        project_paths=[config_path],
    )

    assert config.environment_suffix_target == EnvironmentSuffixTarget.CATALOG
    assert not config.environment_catalog_mapping

    config_path.write_text("""
    gateways:
      warehouse:
        connection:
          type: duckdb

    default_gateway: warehouse

    model_defaults:
      dialect: duckdb

    environment_suffix_target: catalog

    environment_catalog_mapping:
      '.*': "foo"
""")

    with pytest.raises(ConfigError, match=r"mutually exclusive"):
        config = load_config_from_paths(
            Config,
            project_paths=[config_path],
        )


def test_load_python_config_dot_env_vars(tmp_path_factory):
    main_dir = tmp_path_factory.mktemp("python_config")
    config_path = main_dir / "config.py"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig, ModelDefaultsConfig
config = Config(gateways={"duckdb_gateway": GatewayConfig(connection=DuckDBConnectionConfig())}, model_defaults=ModelDefaultsConfig(dialect=''))
        """
        )

    # The environment variable value from the dot env file should be set
    # SQLMESH__ variables override config fields directly if they follow the naming structure
    dot_path = main_dir / ".env"
    with open(dot_path, "w", encoding="utf-8") as fd:
        fd.write(
            """SQLMESH__GATEWAYS__DUCKDB_GATEWAY__STATE_CONNECTION__TYPE="bigquery"
SQLMESH__GATEWAYS__DUCKDB_GATEWAY__STATE_CONNECTION__CHECK_IMPORT="false"
SQLMESH__DEFAULT_GATEWAY="duckdb_gateway"
        """
        )

    # Use mock.patch.dict to isolate environment variables between the tests
    with mock.patch.dict(os.environ, {}, clear=True):
        configs = load_configs(
            "config",
            Config,
            paths=[main_dir],
        )

    assert next(iter(configs.values())) == Config(
        gateways={
            "duckdb_gateway": GatewayConfig(
                connection=DuckDBConnectionConfig(),
                state_connection=BigQueryConnectionConfig(check_import=False),
            ),
        },
        model_defaults=ModelDefaultsConfig(dialect=""),
        default_gateway="duckdb_gateway",
    )


def test_load_yaml_config_dot_env_vars(tmp_path_factory):
    main_dir = tmp_path_factory.mktemp("yaml_config")
    config_path = main_dir / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """gateways:
  duckdb_gateway:
    connection:
      type: duckdb
      catalogs:
        local: local.db
        cloud_sales: {{ env_var('S3_BUCKET') }}
      extensions:
        - name: httpfs
      secrets:
        - type: "s3"
          key_id: {{ env_var('S3_KEY') }}
          secret: {{ env_var('S3_SECRET') }}
model_defaults:
  dialect: ""
"""
        )

    # This test checks both using SQLMESH__ prefixed environment variables with underscores
    # and setting a regular environment variable for use with env_var().
    dot_path = main_dir / ".env"
    with open(dot_path, "w", encoding="utf-8") as fd:
        fd.write(
            """S3_BUCKET="s3://metrics_bucket/sales.db"
S3_KEY="S3_KEY_ID"
S3_SECRET="XXX_S3_SECRET_XXX"
SQLMESH__DEFAULT_GATEWAY="duckdb_gateway"
SQLMESH__MODEL_DEFAULTS__DIALECT="athena"
"""
        )

    # Use mock.patch.dict to isolate environment variables between the tests
    with mock.patch.dict(os.environ, {}, clear=True):
        configs = load_configs(
            "config",
            Config,
            paths=[main_dir],
        )

    assert next(iter(configs.values())) == Config(
        gateways={
            "duckdb_gateway": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    catalogs={
                        "local": "local.db",
                        "cloud_sales": "s3://metrics_bucket/sales.db",
                    },
                    extensions=[{"name": "httpfs"}],
                    secrets=[{"type": "s3", "key_id": "S3_KEY_ID", "secret": "XXX_S3_SECRET_XXX"}],
                ),
            ),
        },
        default_gateway="duckdb_gateway",
        model_defaults=ModelDefaultsConfig(dialect="athena"),
    )


def test_load_config_dotenv_directory_not_loaded(tmp_path_factory):
    main_dir = tmp_path_factory.mktemp("config_with_env_dir")
    config_path = main_dir / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """gateways:
  test_gateway:
    connection:
      type: duckdb
      database: test.db
model_defaults:
  dialect: duckdb
"""
        )

    # Create a .env directory instead of a file  to simulate a Python virtual environment
    env_dir = main_dir / ".env"
    env_dir.mkdir()
    (env_dir / "pyvenv.cfg").touch()

    # Also create a regular .env file in another project directory
    other_dir = tmp_path_factory.mktemp("config_with_env_file")
    other_config_path = other_dir / "config.yaml"
    with open(other_config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """gateways:
  test_gateway:
    connection:
      type: duckdb
      database: test.db
model_defaults:
  dialect: duckdb
"""
        )

    env_file = other_dir / ".env"
    with open(env_file, "w", encoding="utf-8") as fd:
        fd.write('TEST_ENV_VAR="from_dotenv_file"')

    # Test that the .env directory doesn't cause an error and is skipped
    with mock.patch.dict(os.environ, {}, clear=True):
        load_configs(
            "config",
            Config,
            paths=[main_dir],
        )
        # Should succeed without loading any env vars from the directory
        assert "TEST_ENV_VAR" not in os.environ

    # Test that a real .env file is still loaded properly
    with mock.patch.dict(os.environ, {}, clear=True):
        load_configs(
            "config",
            Config,
            paths=[other_dir],
        )
        # The env var should be loaded from the file
        assert os.environ.get("TEST_ENV_VAR") == "from_dotenv_file"


def test_load_yaml_config_custom_dotenv_path(tmp_path_factory):
    main_dir = tmp_path_factory.mktemp("yaml_config_2")
    config_path = main_dir / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as fd:
        fd.write(
            """gateways:
  test_gateway:
    connection:
      type: duckdb
      database: {{ env_var('DB_NAME') }}
"""
        )

    # Create a custom dot env file in a different location
    custom_env_dir = tmp_path_factory.mktemp("custom_env")
    custom_env_path = custom_env_dir / ".my_env"
    with open(custom_env_path, "w", encoding="utf-8") as fd:
        fd.write(
            """DB_NAME="custom_database.db"
SQLMESH__DEFAULT_GATEWAY="test_gateway"
SQLMESH__MODEL_DEFAULTS__DIALECT="postgres"
"""
        )

    # Test that without custom dotenv path, env vars are not loaded
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(
            ConfigError, match=r"Default model SQL dialect is a required configuratio*"
        ):
            load_configs(
                "config",
                Config,
                paths=[main_dir],
            )

    # Test that with custom dotenv path, env vars are loaded correctly
    with mock.patch.dict(os.environ, {}, clear=True):
        configs = load_configs(
            "config",
            Config,
            paths=[main_dir],
            dotenv_path=custom_env_path,
        )

    assert next(iter(configs.values())) == Config(
        gateways={
            "test_gateway": GatewayConfig(
                connection=DuckDBConnectionConfig(
                    database="custom_database.db",
                ),
            ),
        },
        default_gateway="test_gateway",
        model_defaults=ModelDefaultsConfig(dialect="postgres"),
    )


@pytest.mark.parametrize(
    "convention_str, expected",
    [
        (None, TableNamingConvention.SCHEMA_AND_TABLE),
        ("schema_and_table", TableNamingConvention.SCHEMA_AND_TABLE),
        ("table_only", TableNamingConvention.TABLE_ONLY),
        ("hash_md5", TableNamingConvention.HASH_MD5),
    ],
)
def test_physical_table_naming_convention(
    convention_str: t.Optional[str], expected: t.Optional[TableNamingConvention], tmp_path: Path
):
    config_part = f"physical_table_naming_convention: {convention_str}" if convention_str else ""
    (tmp_path / "config.yaml").write_text(f"""
gateways:
  test_gateway:
    connection:
      type: duckdb
model_defaults:
  dialect: duckdb
{config_part}
    """)

    config = load_config_from_paths(Config, project_paths=[tmp_path / "config.yaml"])
    assert config.physical_table_naming_convention == expected


def test_load_configs_includes_sqlmesh_yaml(tmp_path: Path):
    for extension in ("yaml", "yml"):
        config_file = tmp_path / f"sqlmesh.{extension}"
        config_file.write_text("""
model_defaults:
  start: '2023-04-05'
  dialect: bigquery""")

        configs = load_configs(config=None, config_type=Config, paths=[tmp_path])
        assert len(configs) == 1

        config: Config = list(configs.values())[0]

        assert config.model_defaults.start == "2023-04-05"
        assert config.model_defaults.dialect == "bigquery"

        config_file.unlink()


def test_load_configs_without_main_connection(tmp_path: Path):
    # this is for DBT projects where the main connection is defined in profiles.yml
    # but we also need to be able to specify the sqlmesh state connection without editing any DBT files
    # and without also duplicating the main connection
    config_file = tmp_path / "sqlmesh.yaml"
    with config_file.open("w") as f:
        yaml.dump(
            {
                "gateways": {"": {"state_connection": {"type": "duckdb", "database": "state.db"}}},
                "model_defaults": {"dialect": "duckdb", "start": "2020-01-01"},
            },
            f,
        )

    configs = list(load_configs(config=None, config_type=Config, paths=[tmp_path]).values())
    assert len(configs) == 1

    config = configs[0]
    state_connection_config = config.get_state_connection()
    assert isinstance(state_connection_config, DuckDBConnectionConfig)
    assert state_connection_config.database == "state.db"


def test_load_configs_in_dbt_project_without_config_py(tmp_path: Path):
    # this is when someone either:
    # - inits a dbt project for sqlmesh, which creates a sqlmesh.yaml file
    # - uses the sqlmesh_dbt cli for the first time, which runs init if the config doesnt exist, which creates a config
    # when in pure yaml mode, sqlmesh should be able to auto-detect the presence of DBT and select the DbtLoader instead
    # of the main loader
    (tmp_path / "dbt_project.yml").write_text("""
name: jaffle_shop
    """)

    (tmp_path / "profiles.yml").write_text("""
jaffle_shop:

  target: dev
  outputs:
    dev:
      type: duckdb
      path: 'jaffle_shop.duckdb'
    """)

    (tmp_path / "sqlmesh.yaml").write_text("""
gateways:
  dev:
    state_connection:
      type: duckdb
      database: state.db
model_defaults:
  start: '2020-01-01'
""")

    configs = list(load_configs(config=None, config_type=Config, paths=[tmp_path]).values())
    assert len(configs) == 1

    config = configs[0]
    assert config.loader == DbtLoader

    assert list(config.gateways) == ["dev"]

    # main connection
    connection_config = config.get_connection()
    assert connection_config
    assert isinstance(connection_config, DuckDBConnectionConfig)
    assert connection_config.database == "jaffle_shop.duckdb"  # from dbt profiles.yml

    # state connection
    state_connection_config = config.get_state_connection()
    assert state_connection_config
    assert isinstance(state_connection_config, DuckDBConnectionConfig)
    assert state_connection_config.database == "state.db"  # from sqlmesh.yaml

    # model_defaults
    assert config.model_defaults.dialect == "duckdb"  # from dbt profiles.yml
    assert config.model_defaults.start == "2020-01-01"  # from sqlmesh.yaml
