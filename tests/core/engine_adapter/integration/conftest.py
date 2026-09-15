from __future__ import annotations

import typing as t
import pytest
import pathlib
import os
import logging
from pytest import FixtureRequest

from sqlmesh import Config, EngineAdapter
from sqlmesh.core.constants import SQLMESH_PATH
from sqlmesh.core.config.connection import (
    ConnectionConfig,
    AthenaConnectionConfig,
    DuckDBConnectionConfig,
)
from sqlmesh.core.engine_adapter import AthenaEngineAdapter
from sqlmesh.core.config import load_config_from_paths

from tests.core.engine_adapter.integration import (
    TestContext,
    generate_pytest_params,
    ENGINES,
    IntegrationTestEngine,
)

logger = logging.getLogger(__name__)


@pytest.fixture
def config(tmp_path: pathlib.Path) -> Config:
    return load_config_from_paths(
        Config,
        project_paths=[
            pathlib.Path(os.path.join(os.path.dirname(__file__), "config.yaml")),
        ],
        personal_paths=[(SQLMESH_PATH / "config.yaml").expanduser()],
        variables={"tmp_path": str(tmp_path)},
    )


@pytest.fixture
def create_engine_adapter(
    request: pytest.FixtureRequest,
    testrun_uid: str,
    config: Config,
) -> t.Callable[[str, str], EngineAdapter]:
    def _create(engine_name: str, gateway: str) -> EngineAdapter:
        assert gateway in config.gateways
        connection_config = config.gateways[gateway].connection
        assert isinstance(connection_config, ConnectionConfig)

        engine_adapter = connection_config.create_engine_adapter()

        if engine_name == "athena":
            assert isinstance(connection_config, AthenaConnectionConfig)
            assert isinstance(engine_adapter, AthenaEngineAdapter)

            # S3 files need to go into a unique location for each test run
            # This is because DROP TABLE on a Hive table just drops the table from the metastore
            # The files still exist in S3, so if you CREATE TABLE to the same location, the old data shows back up
            # Note that the `testrun_uid` fixture comes from the xdist plugin
            if connection_config.s3_warehouse_location:
                engine_adapter.s3_warehouse_location = os.path.join(
                    connection_config.s3_warehouse_location,
                    f"testrun_{testrun_uid}",
                    request.node.originalname,
                )

        # Trino: If we batch up the requests then when running locally we get a table not found error after creating the
        # table and then immediately after trying to insert rows into it. There seems to be a delay between when the
        # metastore is made aware of the table and when it responds that it exists. I'm hoping this is not an issue
        # in practice on production machines.
        if not engine_name == "trino":
            engine_adapter.DEFAULT_BATCH_SIZE = 1

        # Clear our any local db files that may have been left over from previous runs
        if engine_name == "duckdb":
            assert isinstance(connection_config, DuckDBConnectionConfig)
            for raw_path in [
                v for v in (connection_config.catalogs or {}).values() if isinstance(v, str)
            ]:
                pathlib.Path(raw_path).unlink(missing_ok=True)

        return engine_adapter

    return _create


@pytest.fixture
def create_test_context(
    request: FixtureRequest,
    create_engine_adapter: t.Callable[[str, str], EngineAdapter],
    tmp_path: pathlib.Path,
) -> t.Callable[[IntegrationTestEngine, str, str, str], t.Iterable[TestContext]]:
    def _create(
        engine: IntegrationTestEngine, gateway: str, test_type: str, table_format: str
    ) -> t.Iterable[TestContext]:
        is_remote = request.node.get_closest_marker("remote") is not None

        engine_adapter = create_engine_adapter(engine.engine, gateway)

        ctx = TestContext(
            test_type,
            engine_adapter,
            f"{engine.engine}_{table_format}",
            gateway,
            tmp_path=tmp_path,
            is_remote=is_remote,
        )

        try:
            ctx.init()
        except:
            # pytest-retry doesnt work if there are errors in fixture setup (ref: https://github.com/str0zzapreti/pytest-retry/issues/33 )
            # what we can do is log the exception and return a partially-initialized context to the test, which should
            # throw an exception when it tries to access something that didnt init properly and thus trigger pytest-retry to retry
            logger.exception("Context init failed")

        with ctx.engine_adapter.session({}):
            yield ctx

        try:
            ctx.cleanup()
        except:
            # We need to catch this exception because if there is an error during teardown, pytest-retry aborts immediately
            # instead of retrying
            logger.exception("Context cleanup failed")

    return _create


@pytest.fixture(
    params=list(generate_pytest_params(ENGINES, query=True, show_variant_in_test_id=False))
)
def ctx(
    request: FixtureRequest,
    create_test_context: t.Callable[[IntegrationTestEngine, str], t.Iterable[TestContext]],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)


@pytest.fixture(params=list(generate_pytest_params(ENGINES, query=False, df=True)))
def ctx_df(
    request: FixtureRequest,
    create_test_context: t.Callable[[IntegrationTestEngine, str], t.Iterable[TestContext]],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)


@pytest.fixture(params=list(generate_pytest_params(ENGINES, query=True, df=True)))
def ctx_query_and_df(
    request: FixtureRequest,
    create_test_context: t.Callable[[IntegrationTestEngine, str], t.Iterable[TestContext]],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)
