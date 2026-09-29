import pytest
import typing as t
from pathlib import Path
from unittest.mock import patch

import pandas as pd  # noqa: TID253
from pytest_mock.plugin import MockerFixture
from sqlglot import exp, parse_one

from sqlmesh.core import constants as c
from sqlmesh.core.config import Config, DuckDBConnectionConfig, GatewayConfig
from sqlmesh.core.context import Context
from sqlmesh.core.dialect import parse
from sqlmesh.core.model import SqlModel, create_external_model, load_sql_based_model
from sqlmesh.core.model.definition import ExternalModel
from sqlmesh.core.schema_loader import create_external_models_file
from sqlmesh.core.snapshot import SnapshotChangeCategory
from sqlmesh.utils import yaml
from sqlmesh.utils.errors import SQLMeshError


def test_create_external_models(tmp_path, assert_exp_eq):
    config = Config(gateways=GatewayConfig(connection=DuckDBConnectionConfig()))
    context = Context(paths=[tmp_path], config=config)

    # `fruits` is used by DuckDB in the upcoming select query
    fruits = pd.DataFrame(
        [
            {"id": 1, "name": "apple"},
            {"id": 2, "name": "banana"},
        ]
    )

    cursor = context.engine_adapter.cursor
    cursor.execute("CREATE SCHEMA sushi")
    cursor.execute("CREATE TABLE sushi.raw_fruits AS SELECT * FROM fruits")

    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name sushi.fruits,
            kind FULL,
        );

        SELECT name FROM sushi.raw_fruits
    """,
        ),
        default_catalog="memory",
    )

    context.upsert_model(model)
    context.create_external_models()
    assert context.models['"memory"."sushi"."fruits"'].columns_to_types == {
        "name": exp.DataType.build("UNKNOWN")
    }
    context.load()

    model = load_sql_based_model(
        parse(
            """
        MODEL (
            name sushi.fruits,
            kind FULL,
        );

        SELECT * FROM sushi.raw_fruits
    """,
        ),
        default_catalog="memory",
    )

    context.upsert_model(model)
    raw_fruits = context.models['"memory"."sushi"."raw_fruits"']
    assert raw_fruits.kind.is_symbolic
    assert raw_fruits.kind.is_external
    assert raw_fruits.columns_to_types == {
        "id": exp.DataType.build("BIGINT"),
        "name": exp.DataType.build("TEXT"),
    }

    snapshot = context.get_snapshot("sushi.raw_fruits", raise_if_missing=True)
    snapshot.categorize_as(SnapshotChangeCategory.BREAKING)

    fruits = context.models['"memory"."sushi"."fruits"']
    assert not fruits.kind.is_symbolic
    assert not fruits.kind.is_external
    assert fruits.columns_to_types == {
        "id": exp.DataType.build("BIGINT"),
        "name": exp.DataType.build("TEXT"),
    }
    assert_exp_eq(
        fruits.render_query(snapshots=context.snapshots),
        """
        SELECT
          "raw_fruits"."id" AS "id",
          "raw_fruits"."name" AS "name"
        FROM "memory"."sushi"."raw_fruits" AS "raw_fruits"
        """,
    )
    # rerunning create external models should refetch existing external models
    context.create_external_models()
    context.load()
    assert context.models['"memory"."sushi"."raw_fruits"']
    # there was no explicit gateway, so it should not be written to the external models schema
    assert context.models['"memory"."sushi"."raw_fruits"'].gateway is None


def test_gateway_specific_external_models(tmp_path: Path):
    gateways = {
        "dev": GatewayConfig(connection=DuckDBConnectionConfig()),
        "prod": GatewayConfig(connection=DuckDBConnectionConfig()),
    }

    config = Config(gateways=gateways, default_gateway="dev")

    dev_context = Context(paths=[tmp_path], config=config, gateway="dev")
    dev_context.engine_adapter.execute("create schema landing")
    dev_context.engine_adapter.execute("create table landing.dev_source as select 1")
    dev_context.engine_adapter.execute("create schema lake")

    prod_context = Context(paths=[tmp_path], config=config, gateway="prod")
    prod_context.engine_adapter.execute("create schema landing")
    prod_context.engine_adapter.execute("create table landing.prod_source as select 1")
    prod_context.engine_adapter.execute("create schema lake")

    def _create_model(gateway: str):
        return load_sql_based_model(
            parse(
                """
            MODEL (
                name lake.table,
                kind FULL,
            );

            SELECT * FROM landing.@{gateway}_source
            """,
            ),
            variables={"gateway": gateway},
            default_catalog="memory",
        )

    dev_context.upsert_model(_create_model("dev"))
    prod_context.upsert_model(_create_model("prod"))

    dev_context.create_external_models()
    dev_context.load()

    dev_models = dev_context.models
    assert len(dev_models) == 1
    dev_model = t.cast(ExternalModel, dev_models['"memory"."landing"."dev_source"'])
    assert dev_model.gateway == "dev"

    prod_context.create_external_models()
    prod_context.load()
    prod_models = prod_context.models
    prod_model = t.cast(ExternalModel, prod_models['"memory"."landing"."prod_source"'])
    assert prod_model.gateway == "prod"

    # each context can only see models for its own gateway
    # check that models from both gateways present in the file, to show that prod_context.create_external_models() didnt clobber the dev ones
    contents = yaml.load(tmp_path / c.EXTERNAL_MODELS_YAML)

    assert len(contents) == 2
    assert len([c for c in contents if c["name"] == '"memory"."landing"."dev_source"']) == 1
    assert len([c for c in contents if c["name"] == '"memory"."landing"."prod_source"']) == 1


def test_gateway_specific_external_models_mixed_with_others(tmp_path: Path):
    def _init_db(ctx: Context):
        ctx.engine_adapter.execute("create schema landing")
        ctx.engine_adapter.execute("create table landing.source_table as select 1")
        ctx.engine_adapter.execute("create schema lake")

    gateways = {
        "dev": GatewayConfig(connection=DuckDBConnectionConfig()),
        "prod": GatewayConfig(connection=DuckDBConnectionConfig()),
    }

    config = Config(gateways=gateways, default_gateway="dev")

    model_dir = tmp_path / c.MODELS
    model_dir.mkdir()

    with open(model_dir / "table.sql", "w", encoding="utf8") as fd:
        fd.write(
            """
        MODEL (
            name lake.table,
            kind FULL,
        );

        SELECT * FROM landing.source_table
        """,
        )

    ctx = Context(paths=[tmp_path], config=config)  # note: No explicitly defined gateway
    assert ctx.gateway is None
    assert ctx.selected_gateway == "dev"

    _init_db(ctx)

    ctx.load()
    assert len(ctx.models) == 1
    assert '"memory"."lake"."table"' in ctx.models

    ctx.create_external_models()

    # no gateway was specifically chosen; external models should be created against the default gateway
    external_models_filename = tmp_path / c.EXTERNAL_MODELS_YAML
    contents = yaml.load(external_models_filename)
    assert len(contents) == 1
    assert contents[0]["gateway"] == "dev"

    ctx.load()
    assert len(ctx.models) == 2
    assert '"memory"."landing"."source_table"' in ctx.models
    assert '"memory"."lake"."table"' in ctx.models

    # explicitly set --gateway prod
    prod_ctx = Context(paths=[tmp_path], config=config, gateway="prod")
    assert prod_ctx.gateway == "prod"
    assert prod_ctx.selected_gateway == "prod"

    _init_db(prod_ctx)

    prod_ctx.create_external_models()

    # there should now be 2 external models with the same name - one with a gateway=dev and one with gateway=prod
    contents = yaml.load(external_models_filename)
    assert len(contents) == 2
    assert sorted([contents[0]["gateway"], contents[1]["gateway"]]) == ["dev", "prod"]
    assert contents[0]["name"] == contents[1]["name"]

    # check that this doesnt present a problem on load
    prod_ctx.load()

    external_models = [m for _, m in prod_ctx.models.items() if type(m) == ExternalModel]
    assert len(external_models) == 1
    assert external_models[0].name == '"memory"."landing"."source_table"'
    assert external_models[0].gateway == "prod"


def test_gateway_specific_external_models_default_gateway(tmp_path: Path):
    model_0 = {"name": "db.model0", "columns": {"a": "int"}}

    model_1 = {"name": "db.model1", "gateway": "dev", "columns": {"a": "int"}}

    model_2 = {"name": "db.model2", "gateway": "prod", "columns": {"a": "int"}}

    with open(tmp_path / c.EXTERNAL_MODELS_YAML, "w", encoding="utf8") as fd:
        yaml.dump([model_0, model_1, model_2], fd)

    gateways = {
        "dev": GatewayConfig(connection=DuckDBConnectionConfig()),
        "prod": GatewayConfig(connection=DuckDBConnectionConfig()),
    }

    config = Config(gateways=gateways, default_gateway="prod")
    ctx = Context(paths=[tmp_path], config=config)

    ctx.load()

    assert len(ctx.models) == 2
    model_names = list(ctx.models.keys())
    assert '"memory"."db"."model0"' in model_names
    assert '"memory"."db"."model2"' in model_names


def test_create_external_models_no_duplicates(tmp_path: Path):
    config = Config(gateways={"": GatewayConfig(connection=DuckDBConnectionConfig())})

    model_dir = tmp_path / c.MODELS
    model_dir.mkdir()

    with open(model_dir / "table.sql", "w", encoding="utf8") as fd:
        fd.write(
            """
        MODEL (
            name lake.table,
            kind FULL,
        );

        SELECT * FROM landing.source_table
        """,
        )

    ctx = Context(paths=[tmp_path], config=config)
    assert ctx.gateway is None
    ctx.engine_adapter.execute("create schema landing")
    ctx.engine_adapter.execute("create table landing.source_table as select 1")
    ctx.engine_adapter.execute("create schema lake")

    def _load_external_models():
        return yaml.load(tmp_path / c.EXTERNAL_MODELS_YAML)

    ctx.create_external_models()

    assert len(_load_external_models()) == 1

    # check no duplicates when writing the same models
    # (since the file gets mutated and not replaced)
    ctx.create_external_models()

    assert len(_load_external_models()) == 1


def test_create_external_models_skips_models_from_external_models_directory(tmp_path: Path):
    config = Config(gateways={"": GatewayConfig(connection=DuckDBConnectionConfig())})

    model_dir = tmp_path / c.MODELS
    model_dir.mkdir()

    with open(model_dir / "table.sql", "w", encoding="utf8") as fd:
        fd.write(
            """
        MODEL (
            name lake.table,
            kind FULL,
        );

        SELECT * FROM landing.source_table
        """,
        )

    external_models_dir = tmp_path / c.EXTERNAL_MODELS
    external_models_dir.mkdir()

    with open(external_models_dir / "source_table.yaml", "w", encoding="utf8") as fd:
        yaml.dump(
            [
                {
                    "name": "landing.source_table",
                    "columns": {"a": "int"},
                }
            ],
            fd,
        )

    ctx = Context(paths=[tmp_path], config=config)
    ctx.engine_adapter.execute("create schema landing")
    ctx.engine_adapter.execute("create table landing.source_table as select 1 as a")
    ctx.engine_adapter.execute("create schema lake")

    ctx.create_external_models()

    assert yaml.load(tmp_path / c.EXTERNAL_MODELS_YAML) == []

    ctx.load()
    external_models = [model for model in ctx.models.values() if isinstance(model, ExternalModel)]
    assert len(external_models) == 1
    assert external_models[0].fqn == '"memory"."landing"."source_table"'


def test_no_internal_model_conversion(tmp_path: Path, mocker: MockerFixture):
    engine_adapter_mock = mocker.Mock()
    engine_adapter_mock.columns.return_value = {
        "b": exp.DataType.build("text"),
        "a": exp.DataType.build("bigint"),
    }

    state_reader_mock = mocker.Mock()
    state_reader_mock.nodes_exist.return_value = {'"model_b"'}

    model_a = SqlModel(name="a", query=parse_one("select * FROM model_b, tbl_c"))
    model_b = SqlModel(name="b", query=parse_one("select * FROM `tbl-d`", read="bigquery"))

    filename = tmp_path / c.EXTERNAL_MODELS_YAML
    create_external_models_file(
        filename,
        {  # type: ignore
            "a": model_a,
            "b": model_b,
        },
        engine_adapter_mock,
        state_reader_mock,
        "bigquery",
    )

    schema = yaml.load(filename)

    assert len(schema) == 2
    assert schema[0]["name"] == "`tbl-d`"
    assert list(schema[0]["columns"]) == ["b", "a"]
    assert schema[1]["name"] == "`tbl_c`"
    assert list(schema[1]["columns"]) == ["b", "a"]

    for row in schema:
        create_external_model(**row, dialect="bigquery")


def test_missing_table(tmp_path: Path):
    config = Config(gateways=GatewayConfig(connection=DuckDBConnectionConfig()))
    context = Context(paths=[str(tmp_path.absolute())], config=config)
    model = SqlModel(name="a", query=parse_one("select * FROM tbl_source"))

    filename = tmp_path / c.EXTERNAL_MODELS_YAML
    with patch.object(context.console, "log_warning") as mock_logger:
        create_external_models_file(
            filename,
            {"a": model},  # type: ignore
            context.engine_adapter,
            context.state_reader,
            "",
        )
    assert """Unable to get schema for '"tbl_source"'""" in mock_logger.call_args[0][0]

    schema = yaml.load(filename)
    assert len(schema) == 0

    with pytest.raises(SQLMeshError, match=r"""Unable to get schema for '"tbl_source"'.*"""):
        create_external_models_file(
            filename,
            {"a": model},  # type: ignore
            context.engine_adapter,
            context.state_reader,
            "",
            strict=True,
        )
