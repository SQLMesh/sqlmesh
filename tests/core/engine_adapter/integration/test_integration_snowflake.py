import pytest
import typing as t
from datetime import datetime
from pathlib import Path
from pytest import FixtureRequest
from pytest_mock import MockerFixture

import sqlmesh.core.dialect as d
from sqlglot import exp
from sqlmesh import Config, ExecutionContext, model
from sqlglot.helper import seq_get
from sqlglot.optimizer.qualify_columns import quote_identifiers
from sqlmesh.core.config import ModelDefaultsConfig
from sqlmesh.core.engine_adapter import SnowflakeEngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObject
from sqlmesh.core.model import ModelKindName, SqlModel, load_sql_based_model
from sqlmesh.core.plan import Plan
from sqlmesh.core.snapshot import SnapshotId, SnapshotIdBatch
from sqlmesh.core.snapshot.execution_tracker import (
    QueryExecutionContext,
    QueryExecutionTracker,
)
from tests.core.engine_adapter.integration import (
    TestContext,
    generate_pytest_params,
    ENGINES_BY_NAME,
    IntegrationTestEngine,
)


@pytest.fixture(
    params=list(generate_pytest_params(ENGINES_BY_NAME["snowflake"], show_variant_in_test_id=False))
)
def ctx(
    request: FixtureRequest,
    create_test_context: t.Callable[[IntegrationTestEngine, str, str], t.Iterable[TestContext]],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)


@pytest.fixture
def engine_adapter(ctx: TestContext) -> SnowflakeEngineAdapter:
    assert isinstance(ctx.engine_adapter, SnowflakeEngineAdapter)
    return ctx.engine_adapter


def test_get_alter_expressions_includes_clustering(
    ctx: TestContext, engine_adapter: SnowflakeEngineAdapter
):
    clustered_table = ctx.table("clustered_table")
    clustered_differently_table = ctx.table("clustered_differently_table")
    normal_table = ctx.table("normal_table")

    engine_adapter.execute(f"CREATE TABLE {clustered_table} (c1 int, c2 timestamp) CLUSTER BY (c1)")
    engine_adapter.execute(
        f"CREATE TABLE {clustered_differently_table} (c1 int, c2 timestamp) CLUSTER BY (c1, to_date(c2))"
    )
    engine_adapter.execute(f"CREATE TABLE {normal_table} (c1 int, c2 timestamp)")

    assert len(engine_adapter.get_alter_operations(normal_table, normal_table)) == 0
    assert len(engine_adapter.get_alter_operations(clustered_table, clustered_table)) == 0

    # alter table drop clustered
    clustered_to_normal = engine_adapter.get_alter_operations(clustered_table, normal_table)
    assert len(clustered_to_normal) == 1
    assert (
        clustered_to_normal[0].expression.sql(dialect=ctx.dialect)
        == f"ALTER TABLE {clustered_table} DROP CLUSTERING KEY"
    )

    # alter table add clustered
    normal_to_clustered = engine_adapter.get_alter_operations(normal_table, clustered_table)
    assert len(normal_to_clustered) == 1
    assert (
        normal_to_clustered[0].expression.sql(dialect=ctx.dialect)
        == f"ALTER TABLE {normal_table} CLUSTER BY (c1)"
    )

    # alter table change clustering
    clustered_to_clustered_differently = engine_adapter.get_alter_operations(
        clustered_table, clustered_differently_table
    )
    assert len(clustered_to_clustered_differently) == 1
    assert (
        clustered_to_clustered_differently[0].expression.sql(dialect=ctx.dialect)
        == f"ALTER TABLE {clustered_table} CLUSTER BY (c1, TO_DATE(c2))"
    )

    # alter table change clustering
    clustered_differently_to_clustered = engine_adapter.get_alter_operations(
        clustered_differently_table, clustered_table
    )
    assert len(clustered_differently_to_clustered) == 1
    assert (
        clustered_differently_to_clustered[0].expression.sql(dialect=ctx.dialect)
        == f"ALTER TABLE {clustered_differently_table} CLUSTER BY (c1)"
    )


def test_mutating_clustered_by_forward_only(
    ctx: TestContext, engine_adapter: SnowflakeEngineAdapter
):
    model_name = ctx.table("TEST")

    sqlmesh = ctx.create_context()

    def _create_model(**kwargs: t.Any) -> SqlModel:
        extra_props = "\n".join([f"{k} {v}," for k, v in kwargs.items()])
        return t.cast(
            SqlModel,
            load_sql_based_model(
                d.parse(
                    f"""
                MODEL (
                    name {model_name},
                    kind INCREMENTAL_BY_TIME_RANGE (
                        time_column PARTITIONDATE
                    ),
                    {extra_props}
                    start '2021-01-01',
                    cron '@daily',
                    dialect 'snowflake'
                );

                select 1 as ID, current_timestamp() as PARTITIONDATE
                """
                )
            ),
        )

    def _get_data_object(table: exp.Table) -> DataObject:
        data_object = seq_get(engine_adapter.get_data_objects(table.db, {table.name}), 0)
        if not data_object:
            raise ValueError(f"Expected metadata for {table}")
        return data_object

    m1 = _create_model()
    m2 = _create_model(clustered_by="PARTITIONDATE")
    m3 = _create_model(clustered_by="(ID, PARTITIONDATE)")

    # Initial plan - non-clustered table
    sqlmesh.upsert_model(m1)
    plan_1: Plan = sqlmesh.plan(auto_apply=True, no_prompts=True)
    assert len(plan_1.snapshots) == 1
    target_table_1 = exp.to_table(list(plan_1.snapshots.values())[0].table_name())
    quote_identifiers(target_table_1)

    assert not _get_data_object(target_table_1).is_clustered

    # Next plan - add clustering key (non-clustered -> clustered)
    sqlmesh.upsert_model(m2)
    plan_2: Plan = sqlmesh.plan(auto_apply=True, no_prompts=True, forward_only=True)
    assert len(plan_2.snapshots) == 1
    target_table_2 = exp.to_table(list(plan_2.snapshots.values())[0].table_name())
    quote_identifiers(target_table_2)

    assert target_table_1 == target_table_2

    metadata = _get_data_object(target_table_1)
    assert metadata.is_clustered
    assert metadata.clustering_key == 'LINEAR("PARTITIONDATE")'

    # Next plan - change clustering key (clustered -> clustered differently)
    sqlmesh.upsert_model(m3)
    plan_3: Plan = sqlmesh.plan(auto_apply=True, no_prompts=True, forward_only=True)
    assert len(plan_3.snapshots) == 1
    target_table_3 = exp.to_table(list(plan_3.snapshots.values())[0].table_name())
    quote_identifiers(target_table_3)

    assert target_table_1 == target_table_3

    metadata = _get_data_object(target_table_1)
    assert metadata.is_clustered
    assert metadata.clustering_key == 'LINEAR("ID", "PARTITIONDATE")'

    # Next plan - drop clustering key
    sqlmesh.upsert_model(m1)
    plan_4: Plan = sqlmesh.plan(auto_apply=True, no_prompts=True, forward_only=True)
    assert len(plan_4.snapshots) == 1
    target_table_4 = exp.to_table(list(plan_4.snapshots.values())[0].table_name())
    quote_identifiers(target_table_4)

    assert target_table_1 == target_table_4

    metadata = _get_data_object(target_table_1)
    assert not metadata.is_clustered


@pytest.mark.skip(reason="External volume LIST privileges not configured for CI test databases")
def test_create_iceberg_table(ctx: TestContext) -> None:
    # Note: this test relies on a default Catalog and External Volume being configured in Snowflake
    # ref: https://docs.snowflake.com/en/user-guide/tables-iceberg-configure-catalog-integration#set-a-default-catalog-at-the-account-database-or-schema-level
    # ref: https://docs.snowflake.com/en/user-guide/tables-iceberg-configure-external-volume#set-a-default-external-volume-at-the-account-database-or-schema-level
    # This has been done on the Snowflake account used by CI

    model_name = ctx.table("TEST")
    managed_model_name = ctx.table("TEST_DYNAMIC")
    sqlmesh = ctx.create_context()

    model = load_sql_based_model(
        d.parse(f"""
            MODEL (
                name {model_name},
                kind FULL,
                table_format iceberg,
                dialect 'snowflake'
            );

            select 1 as "ID", 'foo' as "NAME";
            """)
    )

    managed_model = load_sql_based_model(
        d.parse(f"""
            MODEL (
                name {managed_model_name},
                kind MANAGED,
                physical_properties (
                    target_lag = '20 minutes'
                ),
                table_format iceberg,
                dialect 'snowflake'
            );

            select "ID", "NAME" from {model_name};
            """)
    )

    sqlmesh.upsert_model(model)
    sqlmesh.upsert_model(managed_model)

    result = sqlmesh.plan(auto_apply=True)

    assert len(result.new_snapshots) == 2


def test_snowpark_concurrency(ctx: TestContext) -> None:
    from snowflake.snowpark import DataFrame

    table = ctx.table("my_model")

    # this model will insert 10 records in batches of 1, with 4 batches at a time running concurrently
    @model(
        name=table.sql(),
        kind=dict(
            name=ModelKindName.INCREMENTAL_BY_TIME_RANGE,
            time_column="ds",
            batch_size=1,
            batch_concurrency=4,
        ),
        columns={"id": "int", "ds": "date"},
        start="2020-01-01",
        end="2020-01-10",
    )
    def execute(context: ExecutionContext, start: datetime, **kwargs) -> DataFrame:
        if snowpark := context.snowpark:
            return snowpark.create_dataframe([(start.day, start.date())], schema=["id", "ds"])

        raise ValueError("Snowpark not present!")

    m = model.get_registry()[table.sql().lower()].model(
        module_path=Path("."), path=Path("."), dialect="snowflake"
    )

    sqlmesh = ctx.create_context()

    # verify that we are actually running in multithreaded mode
    assert sqlmesh.concurrent_tasks > 1
    assert ctx.engine_adapter._multithreaded

    sqlmesh.upsert_model(m)

    plan = sqlmesh.plan(auto_apply=True)

    assert len(plan.new_snapshots) == 1

    query = exp.select("*").from_(table)
    df = ctx.engine_adapter.fetchdf(query, quote_identifiers=True)
    assert len(df) == 10


def test_create_drop_catalog(ctx: TestContext, engine_adapter: SnowflakeEngineAdapter):
    non_sqlmesh_managed_catalog = ctx.add_test_suffix("external_catalog")
    sqlmesh_managed_catalog = ctx.add_test_suffix("env_dev")

    initial_catalog = engine_adapter.get_current_catalog()
    assert initial_catalog

    ctx.create_catalog(
        non_sqlmesh_managed_catalog
    )  # create via TestContext so the sqlmesh_managed comment doesnt get added
    ctx._catalogs.append(sqlmesh_managed_catalog)  # so it still gets cleaned up if the test fails

    engine_adapter.create_catalog(
        sqlmesh_managed_catalog
    )  # create via EngineAdapter so the sqlmesh_managed comment is added

    def fetch_database_names() -> t.Set[str]:
        engine_adapter.set_current_catalog(initial_catalog)
        return {
            str(r[0])
            for r in engine_adapter.fetchall(
                f"select database_name from information_schema.databases where database_name like '%{ctx.test_id}'"
            )
        }

    assert fetch_database_names() == {non_sqlmesh_managed_catalog, sqlmesh_managed_catalog}

    engine_adapter.drop_catalog(
        non_sqlmesh_managed_catalog
    )  # no-op: catalog is not SQLMesh-managed
    assert fetch_database_names() == {non_sqlmesh_managed_catalog, sqlmesh_managed_catalog}

    engine_adapter.drop_catalog(sqlmesh_managed_catalog)  # works, catalog is SQLMesh-managed
    assert fetch_database_names() == {non_sqlmesh_managed_catalog}


def test_rows_tracker(
    ctx: TestContext, engine_adapter: SnowflakeEngineAdapter, mocker: MockerFixture
):
    sqlmesh = ctx.create_context()
    tracker = QueryExecutionTracker()

    add_execution_spy = mocker.spy(QueryExecutionContext, "add_execution")

    with tracker.track_execution(
        SnapshotIdBatch(snapshot_id=SnapshotId(name="a", identifier="a"), batch_id=0)
    ):
        # Snowflake doesn't report row counts for CTAS, so this should not be tracked
        engine_adapter._create_table("a", exp.select("1 as id"))

    assert add_execution_spy.call_count == 0

    stats = tracker.get_execution_stats(
        SnapshotIdBatch(snapshot_id=SnapshotId(name="a", identifier="a"), batch_id=0)
    )
    assert stats is not None
    assert stats.total_rows_processed is None
    assert stats.total_bytes_processed is None


def test_unit_test(tmp_path: Path, ctx: TestContext):
    models_path = tmp_path / "models"
    tests_path = tmp_path / "tests"

    models_path.mkdir()
    tests_path.mkdir()

    test_payload = """
test_dummy_model:
  model: s.dummy
  inputs:
    s.src_table:
      rows:
        - c: 1
  outputs:
    query:
      - c: 1
    """

    (models_path / "dummy_model.sql").write_text(f"MODEL (name s.dummy); SELECT c FROM s.src_table")
    (tests_path / "test_dummy_model.yaml").write_text(test_payload)

    def _config_mutator(gateway_name: str, config: Config):
        config.model_defaults = ModelDefaultsConfig(dialect="snowflake")
        test_connection = config.gateways[gateway_name].connection.copy()  # type: ignore

        # Force the database to lowercase to test that we normalize (if we didn't, the test would fail)
        test_connection.database = test_connection.database.lower()  # type: ignore
        config.gateways[gateway_name].test_connection = test_connection

    sqlmesh = ctx.create_context(path=tmp_path, config_mutator=_config_mutator)

    test_conn = sqlmesh.config.get_test_connection(ctx.gateway)
    assert test_conn.type_ == "snowflake"

    catalog = test_conn.get_catalog()
    assert catalog is not None and catalog.islower()

    test_results = sqlmesh.test()
    assert not test_results.errors
