from __future__ import annotations


import datetime
import logging
import typing as t
import uuid
from contextlib import nullcontext
from pathlib import Path
from shutil import copytree, rmtree
from tempfile import TemporaryDirectory
from unittest import mock
from unittest.mock import PropertyMock
import os
import shutil

import duckdb  # noqa: TID253
import pandas as pd  # noqa: TID253
import pytest
from pytest_mock.plugin import MockerFixture
from sqlglot import exp, maybe_parse, parse_one
from sqlglot.dialects.dialect import DialectType
from sqlglot.helper import ensure_list
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sqlmesh.core.config import Config, BaseDuckDBConnectionConfig, DuckDBConnectionConfig
from sqlmesh.core.config.connection import ConnectionConfig
from sqlmesh.core.context import Context
from sqlmesh.core.engine_adapter import MSSQLEngineAdapter, SparkEngineAdapter
from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.environment import EnvironmentNamingInfo
from sqlmesh.core import lineage
from sqlmesh.core.macros import macro
from sqlmesh.core.model import IncrementalByTimeRangeKind, SqlModel, model
from sqlmesh.core.model.kind import OnDestructiveChange, OnAdditiveChange
from sqlmesh.core.plan import BuiltInPlanEvaluator, Plan, stages as plan_stages
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Node,
    Snapshot,
    SnapshotChangeCategory,
    SnapshotDataVersion,
    SnapshotFingerprint,
)
from sqlmesh.utils import random_id
from sqlmesh.utils.date import TimeLike, to_date
from sqlmesh.utils.windows import IS_WINDOWS, fix_windows_path
from sqlmesh.core.engine_adapter.shared import CatalogSupport

T = t.TypeVar("T", bound=EngineAdapter)


class DuckDBMetadata:
    def __init__(self, engine_adapter: EngineAdapter):
        assert engine_adapter.dialect == "duckdb"
        self.engine_adapter = engine_adapter

    @classmethod
    def from_context(cls, context: Context):
        return cls(engine_adapter=context.engine_adapter)

    @property
    def tables(self) -> t.List[exp.Table]:
        qualified_tables = self.qualified_tables
        for table in qualified_tables:
            table.set("db", None)
            table.set("catalog", None)
        return qualified_tables

    @property
    def qualified_tables(self) -> t.List[exp.Table]:
        return sorted(
            [
                exp.to_table(x, dialect="duckdb")
                for x in self._get_single_col(
                    f"SELECT table_catalog || '.' || table_schema || '.' || table_name as qualified_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' AND {self._system_schema_filter('table_schema')}",
                    "qualified_name",
                    self.engine_adapter,
                )
            ],
            key=lambda x: x.sql(),
        )

    @property
    def views(self) -> t.List[exp.Table]:
        qualified_views = self.qualified_views
        for view in qualified_views:
            view.set("db", None)
            view.set("catalog", None)
        return qualified_views

    @property
    def qualified_views(self) -> t.List[exp.Table]:
        return sorted(
            [
                exp.to_table(x, dialect="duckdb")
                for x in self._get_single_col(
                    f"SELECT table_catalog || '.' || table_schema || '.' || table_name as qualified_name FROM information_schema.tables WHERE table_type = 'VIEW' AND {self._system_schema_filter('table_schema')}",
                    "qualified_name",
                    self.engine_adapter,
                )
            ],
            key=lambda x: x.sql(),
        )

    @property
    def schemas(self) -> t.List[str]:
        return self.schemas_in_catalog(self.engine_adapter.get_current_catalog() or "")

    def schemas_in_catalog(self, catalog_name: str) -> t.List[str]:
        return self._get_single_col(
            f"SELECT schema_name FROM information_schema.schemata WHERE catalog_name = '{catalog_name}' and {self._system_schema_filter('schema_name')}",
            "schema_name",
            self.engine_adapter,
        )

    @property
    def catalogs(self) -> t.Set[str]:
        return set(
            self._get_single_col(
                f"SELECT database_name FROM duckdb_databases() WHERE internal=false",
                "database_name",
                self.engine_adapter,
            )
        )

    def _system_schema_filter(self, col: str) -> str:
        return f"{col} not in ('information_schema', 'pg_catalog', 'main')"

    @staticmethod
    def _get_single_col(query: str, col: str, engine_adapter: EngineAdapter) -> t.List[t.Any]:
        return list(engine_adapter.fetchdf(query)[col].to_dict().values())


class SushiDataValidator:
    def __init__(self, engine_adapter: EngineAdapter, sushi_schema_name: str):
        self.engine_adapter = engine_adapter
        self.sushi_schema_name = sushi_schema_name

    @classmethod
    def from_context(cls, context: Context, sushi_schema_name: str = "sushi"):
        return cls(engine_adapter=context.engine_adapter, sushi_schema_name=sushi_schema_name)

    def validate(
        self,
        model_name: str,
        start: TimeLike,
        end: TimeLike,
        *,
        env_name: t.Optional[str] = None,
        dialect: t.Optional[str] = None,
        environment_naming_info: t.Optional[EnvironmentNamingInfo] = None,
    ) -> t.Dict[t.Any, t.Any]:
        if (
            env_name
            and dialect
            and environment_naming_info
            and environment_naming_info.normalize_name
        ):
            # if the environment_naming_info was configured to normalize names, then Snapshot.qualified_view_name.table_for_enviromnent()
            # returns schemas that contain the environment_name normalised for that engine
            #
            # in practice, this means "test_prod" becomes "TEST_PROD" on some engines so the final views are named like:
            #
            # "sushi__TEST_PROD"."waiter_as_customer_by_day"
            #
            # instead of:
            #
            # "sushi__test_prod"."waiter_as_customer_by_day"
            #
            # this matters for the reading the data back below to validate it
            env_name = normalize_identifiers(env_name, dialect=dialect).name

        """
        Both start and end are inclusive.
        """
        if model_name in (
            f"{self.sushi_schema_name}.customer_revenue_lifetime",
            "sushi.customer_revenue_lifetime",
        ):
            env_name = f"__{env_name}" if env_name else ""
            full_table_path = f"{self.sushi_schema_name}{env_name}.customer_revenue_lifetime"
            query = f"SELECT event_date, count(*) AS the_count FROM {full_table_path} group by event_date order by 2 desc, 1 desc"
            results = self.engine_adapter.fetchdf(
                parse_one(query), quote_identifiers=True
            ).to_dict()
            start_date, end_date = to_date(start), to_date(end)
            num_days_diff = (end_date - start_date).days + 1
            assert len(results["event_date"]) == num_days_diff

            # this creates Pandas Timestamp objects
            expected_dates = [
                pd.to_datetime(end_date - datetime.timedelta(days=x)) for x in range(num_days_diff)
            ]
            # all engines but duckdb and clickhouse fetch dates as datetime.date objects
            if dialect and dialect not in ("duckdb", "clickhouse"):
                expected_dates = [x.date() for x in expected_dates]  # type: ignore
            assert list(results["event_date"].values()) == expected_dates

            return results
        raise NotImplementedError(f"Unknown model_name: {model_name}")


def pytest_collection_modifyitems(items, *args, **kwargs):
    test_type_markers = {
        "fast",
        "slow",
        "docker",
        "remote",
        "isolated",
        "registry_isolation",
        "dialect_isolated",
    }
    for item in items:
        for marker in item.iter_markers():
            if marker.name in test_type_markers:
                break
        else:
            # if no test type marker is found, assume fast test
            item.add_marker("fast")


# Ignore all local config files
@pytest.fixture(scope="session", autouse=True)
def ignore_local_config_files():
    with mock.patch("sqlmesh.core.constants.SQLMESH_PATH", Path(TemporaryDirectory().name)):
        yield


@pytest.fixture(scope="module", autouse=True)
def rescope_global_macros(request):
    existing_registry = macro.get_registry().copy()
    yield
    macro.set_registry(existing_registry)


@pytest.fixture(scope="module", autouse=True)
def rescope_global_models(request):
    existing_registry = model.get_registry().copy()
    yield
    model.set_registry(existing_registry)


@pytest.fixture(scope="function", autouse=True)
def rescope_duckdb_classvar(request):
    BaseDuckDBConnectionConfig._data_file_to_adapter = {}
    yield


@pytest.fixture(scope="function", autouse=True)
def rescope_log_handlers():
    logging.getLogger().handlers.clear()
    yield


@pytest.fixture(scope="function", autouse=True)
def rescope_lineage_cache(request):
    lineage.CACHE.clear()
    yield


@pytest.fixture(autouse=True)
def reset_console():
    from sqlmesh.core.console import set_console, NoopConsole, get_console

    orig_console = get_console()
    set_console(NoopConsole())
    yield
    set_console(orig_console)


@pytest.fixture
def duck_conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def push_plan(context: Context, plan: Plan) -> None:
    plan_evaluator = BuiltInPlanEvaluator(
        context.state_sync,
        context.snapshot_evaluator,
        context.create_scheduler,
        context.default_catalog,
    )
    deployability_index = DeployabilityIndex.create(context.snapshots.values())
    evaluatable_plan = plan.to_evaluatable().copy(update={"skip_backfill": True})
    stages = plan_stages.build_plan_stages(
        evaluatable_plan, context.state_sync, context.default_catalog
    )
    for stage in stages:
        if isinstance(stage, plan_stages.CreateSnapshotRecordsStage):
            plan_evaluator.visit_create_snapshot_records_stage(stage, evaluatable_plan)
        elif isinstance(stage, plan_stages.PhysicalLayerSchemaCreationStage):
            stage.deployability_index = deployability_index
            plan_evaluator.visit_physical_layer_schema_creation_stage(stage, evaluatable_plan)
        elif isinstance(stage, plan_stages.PhysicalLayerUpdateStage):
            stage.deployability_index = deployability_index
            plan_evaluator.visit_physical_layer_update_stage(stage, evaluatable_plan)
        elif isinstance(stage, plan_stages.EnvironmentRecordUpdateStage):
            plan_evaluator.visit_environment_record_update_stage(stage, evaluatable_plan)
        elif isinstance(stage, plan_stages.VirtualLayerUpdateStage):
            stage.deployability_index = deployability_index
            plan_evaluator.visit_virtual_layer_update_stage(stage, evaluatable_plan)
        elif isinstance(stage, plan_stages.FinalizeEnvironmentStage):
            plan_evaluator.visit_finalize_environment_stage(stage, evaluatable_plan)


@pytest.fixture()
def sushi_context_pre_scheduling(init_and_plan_context: t.Callable) -> Context:
    context, plan = init_and_plan_context("examples/sushi")
    push_plan(context, plan)
    return context


@pytest.fixture()
def sushi_context_fixed_date(init_and_plan_context: t.Callable) -> Context:
    context, plan = init_and_plan_context("examples/sushi")

    for model in context.models.values():  # noqa: F402
        if model.start:
            context.upsert_model(model.name, start="2022-01-01")

    plan = context.plan("prod")
    push_plan(context, plan)
    return context


@pytest.fixture()
def sushi_context(init_and_plan_context: t.Callable) -> Context:
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)
    return context


@pytest.fixture()
def sushi_dbt_context(init_and_plan_context: t.Callable) -> Context:
    context, plan = init_and_plan_context("examples/sushi_dbt")

    context.apply(plan)
    return context


@pytest.fixture()
def sushi_test_dbt_context(init_and_plan_context) -> Context:
    from tests.fixtures.dbt.sushi_test.seed_sources import init_raw_schema

    context, plan = init_and_plan_context("tests/fixtures/dbt/sushi_test")
    init_raw_schema(context.engine_adapter)

    context.apply(plan)
    return context


@pytest.fixture()
def sushi_no_default_catalog(mocker: MockerFixture, init_and_plan_context: t.Callable) -> Context:
    mocker.patch(
        "sqlmesh.core.engine_adapter.base.EngineAdapter.default_catalog",
        PropertyMock(return_value=None),
    )
    context, plan = init_and_plan_context("examples/sushi")
    assert context.default_catalog is None
    context.apply(plan)
    return context


@pytest.fixture
def init_and_plan_context(copy_to_temp_path, mocker) -> t.Callable:
    def _make_function(
        paths: str | t.List[str] | Path | t.List[Path], config="test_config"
    ) -> t.Tuple[Context, Plan]:
        sushi_context = Context(paths=copy_to_temp_path(paths), config=config)
        confirm = mocker.patch("sqlmesh.core.console.Confirm")
        confirm.ask.return_value = False

        plan = sushi_context.plan("prod")

        return sushi_context, plan

    return _make_function


@pytest.fixture
def assert_exp_eq() -> t.Callable:
    def _assert_exp_eq(
        source: exp.Expr | str, expected: exp.Expr | str, dialect: DialectType = None
    ) -> None:
        source_exp = maybe_parse(source, dialect=dialect)
        expected_exp = maybe_parse(expected, dialect=dialect)

        if not source_exp:
            raise ValueError(f"Could not parse {source}")
        if not expected_exp:
            raise ValueError(f"Could not parse {expected}")

        if source_exp != expected_exp:
            assert source_exp.sql(dialect=dialect, pretty=True) == expected_exp.sql(
                dialect=dialect, pretty=True
            )
            assert source_exp == expected_exp

    return _assert_exp_eq


@pytest.fixture
def make_snapshot() -> t.Callable:
    def _make_function(node: Node, version: t.Optional[str] = None, **kwargs) -> Snapshot:
        return Snapshot.from_node(
            node,
            **{  # type: ignore
                "nodes": {},
                "ttl": "in 1 week",
                **kwargs,
            },
            version=version,
        )

    return _make_function


@pytest.fixture
def make_snapshot_on_destructive_change(make_snapshot: t.Callable) -> t.Callable:
    def _make_function(
        name: str = "a",
        old_query: str = "select '1' as one, '2' as two, '2022-01-01' ds",
        new_query: str = "select 1 as one, 2 as two, '2022-01-01' ds",
        on_destructive_change: OnDestructiveChange = OnDestructiveChange.ERROR,
    ) -> t.Tuple[Snapshot, Snapshot]:
        snapshot_old = make_snapshot(
            SqlModel(
                name=name,
                dialect="duckdb",
                query=parse_one(old_query),
                kind=IncrementalByTimeRangeKind(
                    time_column="ds", forward_only=True, on_destructive_change=on_destructive_change
                ),
            )
        )

        snapshot = make_snapshot(
            SqlModel(
                name=name,
                dialect="duckdb",
                query=parse_one(new_query),
                kind=IncrementalByTimeRangeKind(
                    time_column="ds", forward_only=True, on_destructive_change=on_destructive_change
                ),
            )
        )
        snapshot.previous_versions = (
            SnapshotDataVersion(
                fingerprint=SnapshotFingerprint(
                    data_hash="test_data_hash",
                    metadata_hash="test_metadata_hash",
                ),
                version="test_version",
                change_category=SnapshotChangeCategory.NON_BREAKING,
                dev_table_suffix="dev",
            ),
        )

        return snapshot_old, snapshot

    return _make_function


@pytest.fixture
def make_snapshot_on_additive_change(make_snapshot: t.Callable) -> t.Callable:
    def _make_function(
        name: str = "a",
        old_query: str = "select '1' as one, '2' as two, '2022-01-01' ds",
        new_query: str = "select '1' as one, '2' as two, '3' as three, '2022-01-01' ds",
        on_additive_change: OnAdditiveChange = OnAdditiveChange.ERROR,
    ) -> t.Tuple[Snapshot, Snapshot]:
        snapshot_old = make_snapshot(
            SqlModel(
                name=name,
                dialect="duckdb",
                query=parse_one(old_query),
                kind=IncrementalByTimeRangeKind(
                    time_column="ds", forward_only=True, on_additive_change=on_additive_change
                ),
            )
        )

        snapshot = make_snapshot(
            SqlModel(
                name=name,
                dialect="duckdb",
                query=parse_one(new_query),
                kind=IncrementalByTimeRangeKind(
                    time_column="ds", forward_only=True, on_additive_change=on_additive_change
                ),
            )
        )
        snapshot.previous_versions = (
            SnapshotDataVersion(
                fingerprint=SnapshotFingerprint(
                    data_hash="test_data_hash",
                    metadata_hash="test_metadata_hash",
                ),
                version="test_version",
                change_category=SnapshotChangeCategory.NON_BREAKING,
                dev_table_suffix="dev",
            ),
        )

        return snapshot_old, snapshot

    return _make_function


@pytest.fixture
def random_name() -> t.Callable:
    return lambda: f"generated_{random_id()}"


@pytest.fixture
def sushi_data_validator(sushi_context: Context) -> SushiDataValidator:
    return SushiDataValidator.from_context(sushi_context)


@pytest.fixture
def sushi_fixed_date_data_validator(sushi_context_fixed_date: Context) -> SushiDataValidator:
    return SushiDataValidator.from_context(sushi_context_fixed_date)


@pytest.fixture
def make_mocked_engine_adapter(mocker: MockerFixture) -> t.Callable:
    def _make_function(
        klass: t.Type[T],
        dialect: t.Optional[str] = None,
        register_comments: bool = True,
        default_catalog: t.Optional[str] = None,
        patch_get_data_objects: bool = True,
        **kwargs: t.Any,
    ) -> T:
        connection_mock = mocker.NonCallableMock()
        cursor_mock = mocker.Mock()
        connection_mock.cursor.return_value = cursor_mock
        cursor_mock.connection.return_value = connection_mock
        adapter = klass(
            lambda *args, **kwargs: connection_mock,
            dialect=dialect or klass.DIALECT,
            register_comments=register_comments,
            default_catalog=default_catalog,
            **kwargs,
        )
        if isinstance(adapter, SparkEngineAdapter):
            mocker.patch(
                "sqlmesh.engines.spark.db_api.spark_session.SparkSessionConnection._spark_major_minor",
                new_callable=PropertyMock(return_value=(3, 5)),
            )
        if isinstance(adapter, MSSQLEngineAdapter):
            mocker.patch(
                "sqlmesh.core.engine_adapter.mssql.MSSQLEngineAdapter.catalog_support",
                new_callable=PropertyMock(return_value=CatalogSupport.REQUIRES_SET_CATALOG),
            )
        if patch_get_data_objects:
            mocker.patch.object(adapter, "_get_data_objects", return_value=[])
        return adapter

    return _make_function


@pytest.fixture
def copy_to_temp_path(tmp_path: Path) -> t.Callable:
    def ignore(src, names):
        # do not copy any sub-dirs if current dir is named one of these
        if Path(src).name in {".cache", "__pycache__", "logs", "data", "target"}:
            return names
        # do not copy sub-dirs named ".cache" for any current dir
        return [name for name in names if name == ".cache"]

    def _make_function(
        paths: t.Union[str, Path, t.List[t.Union[str, Path]], t.Tuple[t.Union[str, Path], ...]],
    ) -> t.List[Path]:
        paths = ensure_list(paths)
        all_paths = [Path(p) for p in paths]
        temp_dirs = []
        for path in all_paths:
            temp_dir = Path(tmp_path) / uuid.uuid4().hex

            if IS_WINDOWS:
                # shutil.copytree just doesnt work properly with the symlinks on Windows, regardless of the `symlinks` setting
                src = str(path.absolute())
                dst = str(temp_dir.absolute())

                # Robocopy flag reference: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy#copy-options
                # /E:      Copy subdirectories, including empty directories
                # /COPY:D  Copy "data" only. In particular, this avoids copying auditing information, which can throw
                #          an error like "ERROR : You do not have the Manage Auditing user right"
                robocopy_cmd = f"robocopy {src} {dst} /E /COPY:D"
                exit_code = os.system(robocopy_cmd)

                # exit code reference: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy#exit-return-codes
                if exit_code > 8:
                    raise Exception(
                        f"robocopy command: '{robocopy_cmd}' failed with exit code: {exit_code}"
                    )

                # after copying, delete the files that would have been ignored
                for root, dirs, _ in os.walk(temp_dir):
                    for dir in dirs:
                        full_dir = fix_windows_path(Path(root) / dir)
                        for ignored in ignore(full_dir, [full_dir]):
                            shutil.rmtree(ignored)

            else:
                copytree(path, temp_dir, symlinks=True, ignore=ignore)

            temp_dirs.append(temp_dir)
        return temp_dirs

    return _make_function


def delete_cache(project_paths: str | t.List[str]) -> None:
    for path in ensure_list(project_paths):
        try:
            rmtree(path + "/.cache")
        except FileNotFoundError:
            pass


@pytest.fixture
def make_temp_table_name(mocker: MockerFixture) -> t.Callable:
    def _make_function(table_name: str, random_id: str) -> exp.Table:
        temp_table = exp.to_table(table_name)
        temp_table.set("this", exp.to_identifier(f"__temp_{temp_table.name}_{random_id}"))
        return temp_table

    return _make_function


@pytest.fixture(scope="function", autouse=True)
def set_default_connection(request):
    request = request.node.get_closest_marker("set_default_connection")
    disable = request and request.kwargs.get("disable")

    if disable:
        ctx = nullcontext()
    else:
        original_get_connection = Config.get_connection

        def _lax_get_connection(self, gateway_name: t.Optional[str] = None) -> ConnectionConfig:
            try:
                connection = original_get_connection(self, gateway_name)
            except:
                connection = DuckDBConnectionConfig()
            return connection

        ctx = mock.patch("sqlmesh.core.config.Config.get_connection", _lax_get_connection)

    with ctx:
        yield
