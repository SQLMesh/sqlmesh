import typing as t
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import numpy as np  # noqa: TID253
import pandas as pd  # noqa: TID253
import pytest
from pytest import FixtureRequest
from pytest_mock import MockerFixture
from pathlib import Path
from sqlmesh.core.engine_adapter import EngineAdapter, PostgresEngineAdapter
from sqlmesh.core.config import Config, DuckDBConnectionConfig
from sqlmesh.core.config.common import VirtualEnvironmentMode
from tests.core.engine_adapter.integration import TestContext
import time_machine
from datetime import timedelta
from sqlmesh.utils.date import to_ds
from sqlglot import exp
from sqlmesh.core.context import Context
from sqlmesh.core.state_sync import CachingStateSync, EngineAdapterStateSync
from sqlmesh.core.snapshot.definition import SnapshotId
from sqlmesh.utils import random_id

from tests.core.engine_adapter.integration import (
    TestContext,
    generate_pytest_params,
    ENGINES_BY_NAME,
    IntegrationTestEngine,
    TEST_SCHEMA,
)


def _cleanup_user(engine_adapter: PostgresEngineAdapter, user_name: str) -> None:
    """Helper function to clean up a PostgreSQL user and all their dependencies."""
    try:
        engine_adapter.execute(f"""
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE usename = '{user_name}' AND pid <> pg_backend_pid()
        """)
        engine_adapter.execute(f'DROP OWNED BY "{user_name}"')
        engine_adapter.execute(f'DROP USER IF EXISTS "{user_name}"')
    except Exception:
        pass


@contextmanager
def create_users(
    engine_adapter: PostgresEngineAdapter, *role_names: str
) -> t.Iterator[t.Dict[str, t.Dict[str, str]]]:
    """Create a set of Postgres users and yield their credentials."""
    created_users = []
    roles = {}

    try:
        for role_name in role_names:
            user_name = f"test_{role_name}"
            _cleanup_user(engine_adapter, user_name)

        for role_name in role_names:
            random_suffix = uuid.uuid4().hex[:6]
            user_name = f"test_{role_name}_{random_suffix}"
            password = random_id()
            engine_adapter.execute(f"CREATE USER \"{user_name}\" WITH PASSWORD '{password}'")
            engine_adapter.execute(f'GRANT USAGE ON SCHEMA public TO "{user_name}"')
            created_users.append(user_name)
            roles[role_name] = {"username": user_name, "password": password}

        yield roles

    finally:
        for user_name in created_users:
            _cleanup_user(engine_adapter, user_name)


def create_engine_adapter_for_role(
    role_credentials: t.Dict[str, str], ctx: TestContext, config: Config
) -> PostgresEngineAdapter:
    """Create a PostgreSQL adapter for a specific role to test authentication and permissions."""
    from sqlmesh.core.config import PostgresConnectionConfig

    gateway = ctx.gateway
    assert gateway in config.gateways
    connection_config = config.gateways[gateway].connection
    assert isinstance(connection_config, PostgresConnectionConfig)

    role_connection_config = PostgresConnectionConfig(
        host=connection_config.host,
        port=connection_config.port,
        database=connection_config.database,
        user=role_credentials["username"],
        password=role_credentials["password"],
        keepalives_idle=connection_config.keepalives_idle,
        connect_timeout=connection_config.connect_timeout,
        role=connection_config.role,
        sslmode=connection_config.sslmode,
        application_name=connection_config.application_name,
    )

    return t.cast(PostgresEngineAdapter, role_connection_config.create_engine_adapter())


@contextmanager
def engine_adapter_for_role(
    role_credentials: t.Dict[str, str], ctx: TestContext, config: Config
) -> t.Iterator[PostgresEngineAdapter]:
    """Context manager that yields a PostgresEngineAdapter and ensures it is closed."""
    adapter = create_engine_adapter_for_role(role_credentials, ctx, config)
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.fixture(params=list(generate_pytest_params(ENGINES_BY_NAME["postgres"])))
def ctx(
    request: FixtureRequest,
    create_test_context: t.Callable[[IntegrationTestEngine, str, str], t.Iterable[TestContext]],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)


@pytest.fixture
def engine_adapter(ctx: TestContext) -> PostgresEngineAdapter:
    assert isinstance(ctx.engine_adapter, PostgresEngineAdapter)
    return ctx.engine_adapter


def test_engine_adapter(ctx: TestContext):
    assert isinstance(ctx.engine_adapter, PostgresEngineAdapter)
    assert ctx.engine_adapter.fetchone("select 1") == (1,)


def test_server_version_psycopg(ctx: TestContext):
    assert isinstance(ctx.engine_adapter, PostgresEngineAdapter)
    assert ctx.engine_adapter.server_version != (0, 0)


def test_janitor_drop_cascade(ctx: TestContext, tmp_path: Path) -> None:
    """
    Scenario:
        Ensure that cleaning up expired table snapshots also cleans up any unexpired view snapshots that depend on them
        - We create a A (table) <- B (view)
        - In dev, we modify A - triggers new version of A and a dev preview of B that both expire in 7 days
        - We advance time by 3 days
        - In dev, we modify B - triggers a new version of B that depends on A but expires 3 days after A
        - In dev, we create B(view) <- C(view) and B(view) <- D(table)
        - We advance time by 5 days so that A has reached its expiry but B, C and D have not
        - We expire dev so that none of these snapshots are promoted and are thus targets for cleanup
        - We run the janitor

    Expected outcome:
        - All the dev versions of A and B should be dropped
        - C should be dropped as well because it's a view that depends on B which was dropped
        - D should not be dropped because while it depends on B which was dropped, it's a table so is still valid after B is dropped
        - We should NOT get a 'ERROR: cannot drop table x because other objects depend on it'

    Note that the references in state to the views that were cascade-dropped by postgres will still exist, this is considered ok
    as applying a plan will recreate the physical objects
    """

    def _all_snapshot_ids(context: Context) -> t.List[SnapshotId]:
        assert isinstance(context.state_sync, CachingStateSync)
        assert isinstance(context.state_sync.state_sync, EngineAdapterStateSync)

        return [
            SnapshotId(name=name, identifier=identifier)
            for name, identifier in context.state_sync.state_sync.engine_adapter.fetchall(
                "select name, identifier from sqlmesh._snapshots"
            )
        ]

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    schema = exp.to_table(ctx.schema(TEST_SCHEMA)).this

    (models_dir / "model_a.sql").write_text(f"""
    MODEL (
        name {schema}.model_a,
        kind FULL
    );
    SELECT 1 as a, 2 as b;
    """)

    (models_dir / "model_b.sql").write_text(f"""
    MODEL (
        name {schema}.model_b,
        kind VIEW
    );
    SELECT a from {schema}.model_a;
    """)

    def _mutate_config(gateway: str, config: Config):
        config.gateways[gateway].state_connection = DuckDBConnectionConfig(
            database=str(tmp_path / "state.db")
        )

    with time_machine.travel("2020-01-01 00:00:00"):
        sqlmesh = ctx.create_context(
            path=tmp_path, config_mutator=_mutate_config, ephemeral_state_connection=False
        )
        sqlmesh.plan(auto_apply=True)

    model_a_snapshot = next(s for n, s in sqlmesh.snapshots.items() if "model_a" in n)
    # expiry is last updated + ttl
    assert timedelta(milliseconds=model_a_snapshot.ttl_ms) == timedelta(weeks=1)
    assert to_ds(model_a_snapshot.updated_ts) == "2020-01-01"
    assert to_ds(model_a_snapshot.expiration_ts) == "2020-01-08"

    model_b_snapshot = next(s for n, s in sqlmesh.snapshots.items() if "model_b" in n)
    assert timedelta(milliseconds=model_b_snapshot.ttl_ms) == timedelta(weeks=1)
    assert to_ds(model_b_snapshot.updated_ts) == "2020-01-01"
    assert to_ds(model_b_snapshot.expiration_ts) == "2020-01-08"

    model_a_prod_snapshot = model_a_snapshot
    model_b_prod_snapshot = model_b_snapshot

    # move forward 1 days
    # new dev environment - touch models to create new snapshots
    # model a / b expiry in prod should remain unmodified
    # model a / b expiry in dev should be as at today
    with time_machine.travel("2020-01-02 00:00:00"):
        (models_dir / "model_a.sql").write_text(f"""
        MODEL (
            name {schema}.model_a,
            kind FULL
        );
        SELECT 1 as a, 2 as b, 3 as c;
        """)

        sqlmesh = ctx.create_context(
            path=tmp_path, config_mutator=_mutate_config, ephemeral_state_connection=False
        )
        sqlmesh.plan(environment="dev", auto_apply=True)

        # should now have 4 snapshots in state - 2x model a and 2x model b
        # the new model b is a dev preview because its upstream model changed
        all_snapshot_ids = _all_snapshot_ids(sqlmesh)
        assert len(all_snapshot_ids) == 4
        assert len([s for s in all_snapshot_ids if "model_a" in s.name]) == 2
        assert len([s for s in all_snapshot_ids if "model_b" in s.name]) == 2

        # context just has the two latest
        assert len(sqlmesh.snapshots) == 2

        # these expire 1 day later than what's in prod
        model_a_snapshot = next(s for n, s in sqlmesh.snapshots.items() if "model_a" in n)
        assert timedelta(milliseconds=model_a_snapshot.ttl_ms) == timedelta(weeks=1)
        assert to_ds(model_a_snapshot.updated_ts) == "2020-01-02"
        assert to_ds(model_a_snapshot.expiration_ts) == "2020-01-09"

        model_b_snapshot = next(s for n, s in sqlmesh.snapshots.items() if "model_b" in n)
        assert timedelta(milliseconds=model_b_snapshot.ttl_ms) == timedelta(weeks=1)
        assert to_ds(model_b_snapshot.updated_ts) == "2020-01-02"
        assert to_ds(model_b_snapshot.expiration_ts) == "2020-01-09"

    # move forward 3 days
    # touch model b in dev but leave model a
    # this bumps the model b expiry but model a remains unchanged, so will expire before model b even though model b depends on it
    with time_machine.travel("2020-01-05 00:00:00"):
        (models_dir / "model_b.sql").write_text(f"""
        MODEL (
            name {schema}.model_b,
            kind VIEW
        );
        SELECT a, 'b' as b from {schema}.model_a;
        """)

        (models_dir / "model_c.sql").write_text(f"""
        MODEL (
            name {schema}.model_c,
            kind VIEW
        );
        SELECT a, 'c' as c from {schema}.model_b;
        """)

        (models_dir / "model_d.sql").write_text(f"""
        MODEL (
            name {schema}.model_d,
            kind FULL
        );
        SELECT a, 'd' as d from {schema}.model_b;
        """)

        sqlmesh = ctx.create_context(
            path=tmp_path, config_mutator=_mutate_config, ephemeral_state_connection=False
        )
        # need run=True to prevent a "start date is greater than end date" error
        # since dev cant exceed what is in prod, and prod has no cadence runs,
        # without run=True this plan gets start=2020-01-04 (now) end=2020-01-01 (last prod interval) which fails
        sqlmesh.plan(environment="dev", auto_apply=True, run=True)

        # should now have 7 snapshots in state - 2x model a, 3x model b, 1x model c and 1x model d
        all_snapshot_ids = _all_snapshot_ids(sqlmesh)
        assert len(all_snapshot_ids) == 7
        assert len([s for s in all_snapshot_ids if "model_a" in s.name]) == 2
        assert len([s for s in all_snapshot_ids if "model_b" in s.name]) == 3
        assert len([s for s in all_snapshot_ids if "model_c" in s.name]) == 1
        assert len([s for s in all_snapshot_ids if "model_d" in s.name]) == 1

        # context just has the 4 latest
        assert len(sqlmesh.snapshots) == 4

        # model a expiry should not have changed
        model_a_snapshot = next(s for n, s in sqlmesh.snapshots.items() if "model_a" in n)
        assert timedelta(milliseconds=model_a_snapshot.ttl_ms) == timedelta(weeks=1)
        assert to_ds(model_a_snapshot.updated_ts) == "2020-01-02"
        assert to_ds(model_a_snapshot.expiration_ts) == "2020-01-09"

        # model b should now expire well after model a
        model_b_snapshot = next(s for n, s in sqlmesh.snapshots.items() if "model_b" in n)
        assert timedelta(milliseconds=model_b_snapshot.ttl_ms) == timedelta(weeks=1)
        assert to_ds(model_b_snapshot.updated_ts) == "2020-01-05"
        assert to_ds(model_b_snapshot.expiration_ts) == "2020-01-12"

        # model c should expire at the same time as model b
        model_c_snapshot = next(s for n, s in sqlmesh.snapshots.items() if "model_c" in n)
        assert to_ds(model_c_snapshot.updated_ts) == to_ds(model_b_snapshot.updated_ts)
        assert to_ds(model_c_snapshot.expiration_ts) == to_ds(model_b_snapshot.expiration_ts)

        # model d should expire at the same time as model b
        model_d_snapshot = next(s for n, s in sqlmesh.snapshots.items() if "model_d" in n)
        assert to_ds(model_d_snapshot.updated_ts) == to_ds(model_b_snapshot.updated_ts)
        assert to_ds(model_d_snapshot.expiration_ts) == to_ds(model_b_snapshot.expiration_ts)

    # move forward to date where after model a has expired but before model b has expired
    # invalidate dev to trigger cleanups
    # run janitor
    # - table model a is expired so will be cleaned up and this will cascade to view model b
    # - view model b is not expired, but because it got cascaded to, this will cascade again to view model c
    # - table model d is a not a view, so even though its parent view model b got dropped, it doesnt need to be dropped
    with time_machine.travel("2020-01-10 00:00:00"):
        sqlmesh = ctx.create_context(
            path=tmp_path, config_mutator=_mutate_config, ephemeral_state_connection=False
        )

        before_snapshot_ids = _all_snapshot_ids(sqlmesh)

        before_objects = ctx.get_metadata_results(f"sqlmesh__{schema}")
        assert set(before_objects.tables) == set(
            [
                exp.to_table(s.table_name()).text("this")
                for s in (model_a_prod_snapshot, model_a_snapshot, model_d_snapshot)
            ]
        )
        assert set(before_objects.views).issuperset(
            [
                exp.to_table(s.table_name()).text("this")
                for s in (model_b_prod_snapshot, model_b_snapshot, model_c_snapshot)
            ]
        )

        sqlmesh.invalidate_environment("dev")
        sqlmesh.run_janitor(ignore_ttl=False)

        after_snapshot_ids = _all_snapshot_ids(sqlmesh)

        assert len(before_snapshot_ids) != len(after_snapshot_ids)

        # Everything should be left in state except the model_a snapshot, which expired
        assert set(after_snapshot_ids) == set(before_snapshot_ids) - set(
            [model_a_snapshot.snapshot_id]
        )

        # In the db, there should be:
        # - the two original snapshots that were in prod, table model_a and view model_b
        # - model d, even though its not promoted in any environment, because it's a table snapshot that hasnt expired yet
        # the view snapshots that depended on model_a should be gone due to the cascading delete
        after_objects = ctx.get_metadata_results(f"sqlmesh__{schema}")
        assert set(after_objects.tables) == set(
            [
                exp.to_table(s.table_name()).text("this")
                for s in (model_a_prod_snapshot, model_d_snapshot)
            ]
        )
        assert after_objects.views == [
            exp.to_table(model_b_prod_snapshot.table_name()).text("this")
        ]


# Grants Integration Tests


def test_grants_plan_target_layer_physical_only(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, tmp_path: Path
):
    with create_users(engine_adapter, "reader") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)

        reader_username = roles["reader"]["username"]

        model_def = f"""
        MODEL (
            name test_schema.physical_grants_model,
            kind FULL,
            grants (
                'select' = ['{reader_username}']
            ),
            grants_target_layer 'physical'
        );
        SELECT 1 as id, 'physical_only' as layer
        """

        (tmp_path / "models" / "physical_grants_model.sql").write_text(model_def)

        context = ctx.create_context(path=tmp_path)
        plan_result = context.plan(auto_apply=True, no_prompts=True)

        assert len(plan_result.new_snapshots) == 1
        snapshot = plan_result.new_snapshots[0]
        physical_table_name = snapshot.table_name()

        physical_grants = engine_adapter._get_current_grants_config(
            exp.to_table(physical_table_name, dialect=engine_adapter.dialect)
        )
        assert physical_grants == {"SELECT": [roles["reader"]["username"]]}

        # Virtual layer should have no grants
        virtual_view_name = f"test_schema.physical_grants_model"
        virtual_grants = engine_adapter._get_current_grants_config(
            exp.to_table(virtual_view_name, dialect=engine_adapter.dialect)
        )
        assert virtual_grants == {}


def test_grants_plan_target_layer_virtual_only(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, tmp_path: Path
):
    with create_users(engine_adapter, "viewer") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)

        viewer_username = roles["viewer"]["username"]

        model_def = f"""
        MODEL (
            name test_schema.virtual_grants_model,
            kind FULL,
            grants (
                'select' = ['{viewer_username}']
            ),
            grants_target_layer 'virtual'
        );
        SELECT 1 as id, 'virtual_only' as layer
        """

        (tmp_path / "models" / "virtual_grants_model.sql").write_text(model_def)

        context = ctx.create_context(path=tmp_path)
        plan_result = context.plan(auto_apply=True, no_prompts=True)

        assert len(plan_result.new_snapshots) == 1
        snapshot = plan_result.new_snapshots[0]
        physical_table_name = snapshot.table_name()

        physical_grants = engine_adapter._get_current_grants_config(
            exp.to_table(physical_table_name, dialect=engine_adapter.dialect)
        )
        # Physical table should have no grants
        assert physical_grants == {}

        virtual_view_name = f"test_schema.virtual_grants_model"
        virtual_grants = engine_adapter._get_current_grants_config(
            exp.to_table(virtual_view_name, dialect=engine_adapter.dialect)
        )
        assert virtual_grants == {"SELECT": [roles["viewer"]["username"]]}


def test_grants_plan_full_refresh_model_via_replace(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, tmp_path: Path
):
    with create_users(engine_adapter, "reader") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)
        (tmp_path / "models" / "full_refresh_model.sql").write_text(
            f"""
            MODEL (
                name test_schema.full_refresh_model,
                kind FULL,
                grants (
                    'SELECT' = ['{roles["reader"]["username"]}']
                ),
                grants_target_layer 'all'
            );
            SELECT 1 as id, 'test_data' as status
            """
        )

        context = ctx.create_context(path=tmp_path)

        plan_result = context.plan(
            "dev",  # this triggers _replace_query_for_model for FULL models
            auto_apply=True,
            no_prompts=True,
        )

        assert len(plan_result.new_snapshots) == 1
        snapshot = plan_result.new_snapshots[0]
        table_name = snapshot.table_name()

        # Physical table
        grants = engine_adapter._get_current_grants_config(
            exp.to_table(table_name, dialect=engine_adapter.dialect)
        )
        assert grants == {"SELECT": [roles["reader"]["username"]]}

        # Virtual view
        dev_view_name = "test_schema__dev.full_refresh_model"
        dev_grants = engine_adapter._get_current_grants_config(
            exp.to_table(dev_view_name, dialect=engine_adapter.dialect)
        )
        assert dev_grants == {"SELECT": [roles["reader"]["username"]]}


def test_grants_plan_incremental_model(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, tmp_path: Path
):
    with create_users(engine_adapter, "reader", "writer") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)

        model_name = "incr_model"
        model_definition = f"""
        MODEL (
            name test_schema.{model_name},
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column ts
            ),
            grants (
                'SELECT' = ['{roles["reader"]["username"]}'],
                'INSERT' = ['{roles["writer"]["username"]}']
            ),
            grants_target_layer 'all'
        );
        SELECT 1 as id, @start_ds::timestamp as ts, 'data' as value
        """

        (tmp_path / "models" / f"{model_name}.sql").write_text(model_definition)

        context = ctx.create_context(path=tmp_path)

        plan_result = context.plan(
            "dev", start="2020-01-01", end="2020-01-01", auto_apply=True, no_prompts=True
        )
        assert len(plan_result.new_snapshots) == 1

        snapshot = plan_result.new_snapshots[0]
        table_name = snapshot.table_name()

        physical_grants = engine_adapter._get_current_grants_config(
            exp.to_table(table_name, dialect=engine_adapter.dialect)
        )
        assert physical_grants.get("SELECT", []) == [roles["reader"]["username"]]
        assert physical_grants.get("INSERT", []) == [roles["writer"]["username"]]

        view_name = f"test_schema__dev.{model_name}"
        view_grants = engine_adapter._get_current_grants_config(
            exp.to_table(view_name, dialect=engine_adapter.dialect)
        )
        assert view_grants.get("SELECT", []) == [roles["reader"]["username"]]
        assert view_grants.get("INSERT", []) == [roles["writer"]["username"]]


def test_grants_plan_clone_environment(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, tmp_path: Path
):
    with create_users(engine_adapter, "reader") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)
        (tmp_path / "models" / "clone_model.sql").write_text(
            f"""
            MODEL (
                name test_schema.clone_model,
                kind FULL,
                grants (
                    'SELECT' = ['{roles["reader"]["username"]}']
                ),
                grants_target_layer 'all'
            );

            SELECT 1 as id, 'data' as value
            """
        )

        context = ctx.create_context(path=tmp_path)
        prod_plan_result = context.plan("prod", auto_apply=True, no_prompts=True)

        assert len(prod_plan_result.new_snapshots) == 1
        prod_snapshot = prod_plan_result.new_snapshots[0]
        prod_table_name = prod_snapshot.table_name()

        # Prod physical table grants
        prod_grants = engine_adapter._get_current_grants_config(
            exp.to_table(prod_table_name, dialect=engine_adapter.dialect)
        )
        assert prod_grants == {"SELECT": [roles["reader"]["username"]]}

        # Prod virtual view grants
        prod_view_name = f"test_schema.clone_model"
        prod_view_grants = engine_adapter._get_current_grants_config(
            exp.to_table(prod_view_name, dialect=engine_adapter.dialect)
        )
        assert prod_view_grants == {"SELECT": [roles["reader"]["username"]]}

        # Create dev environment (cloned from prod)
        context.plan("dev", auto_apply=True, no_prompts=True, include_unmodified=True)

        # Physical table grants should remain unchanged
        prod_grants_after_clone = engine_adapter._get_current_grants_config(
            exp.to_table(prod_table_name, dialect=engine_adapter.dialect)
        )
        assert prod_grants_after_clone == prod_grants

        # Dev virtual view should have the same grants as prod
        dev_view_name = f"test_schema__dev.clone_model"
        dev_view_grants = engine_adapter._get_current_grants_config(
            exp.to_table(dev_view_name, dialect=engine_adapter.dialect)
        )
        assert dev_view_grants == prod_grants


@pytest.mark.parametrize(
    "model_name,kind_config,query,extra_config,needs_seed",
    [
        (
            "grants_full",
            "FULL",
            "SELECT 1 as id, 'unchanged_query' as data",
            "",
            False,
        ),
        (
            "grants_view",
            "VIEW",
            "SELECT 1 as id, 'unchanged_query' as data",
            "",
            False,
        ),
        (
            "grants_incr_time",
            "INCREMENTAL_BY_TIME_RANGE (time_column event_date)",
            "SELECT '2025-09-01'::date as event_date, 1 as id, 'unchanged_query' as data",
            "start '2025-09-01',",
            False,
        ),
        (
            "grants_seed",
            "SEED (path '../seeds/grants_seed.csv')",
            "",
            "",
            True,
        ),
    ],
)
def test_grants_metadata_only_changes(
    engine_adapter: PostgresEngineAdapter,
    ctx: TestContext,
    tmp_path: Path,
    model_name: str,
    kind_config: str,
    query: str,
    extra_config: str,
    needs_seed: bool,
):
    with create_users(engine_adapter, "reader", "writer", "admin") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)

        if needs_seed:
            (tmp_path / "seeds").mkdir(exist_ok=True)
            csv_content = "id,data\\n1,unchanged_query"
            (tmp_path / "seeds" / f"{model_name}.csv").write_text(csv_content)

        initial_model_def = f"""
        MODEL (
            name test_schema.{model_name},
            kind {kind_config},
            {extra_config}
            grants (
                'select' = ['{roles["reader"]["username"]}']
            ),
            grants_target_layer 'all'
        );
        {query}
        """
        (tmp_path / "models" / f"{model_name}.sql").write_text(initial_model_def)

        context = ctx.create_context(path=tmp_path)
        initial_plan_result = context.plan(auto_apply=True, no_prompts=True)

        assert len(initial_plan_result.new_snapshots) == 1
        initial_snapshot = initial_plan_result.new_snapshots[0]

        physical_table_name = initial_snapshot.table_name()
        virtual_view_name = f"test_schema.{model_name}"

        initial_physical_grants = engine_adapter._get_current_grants_config(
            exp.to_table(physical_table_name, dialect=engine_adapter.dialect)
        )
        assert initial_physical_grants == {"SELECT": [roles["reader"]["username"]]}

        initial_virtual_grants = engine_adapter._get_current_grants_config(
            exp.to_table(virtual_view_name, dialect=engine_adapter.dialect)
        )
        assert initial_virtual_grants == {"SELECT": [roles["reader"]["username"]]}

        # Metadata-only change: update grants only using upsert_model
        existing_model = context.get_model(f"test_schema.{model_name}")
        context.upsert_model(
            existing_model,
            grants={
                "select": [roles["writer"]["username"], roles["admin"]["username"]],
                "insert": [roles["admin"]["username"]],
            },
        )
        second_plan_result = context.plan(auto_apply=True, no_prompts=True)

        expected_grants = {
            "SELECT": [roles["writer"]["username"], roles["admin"]["username"]],
            "INSERT": [roles["admin"]["username"]],
        }

        # For seed models, grant changes rebuild the entire table, so it will create a new physical table
        if model_name == "grants_seed" and second_plan_result.new_snapshots:
            updated_snapshot = second_plan_result.new_snapshots[0]
            physical_table_name = updated_snapshot.table_name()

        updated_physical_grants = engine_adapter._get_current_grants_config(
            exp.to_table(physical_table_name, dialect=engine_adapter.dialect)
        )
        assert set(updated_physical_grants.get("SELECT", [])) == set(expected_grants["SELECT"])
        assert updated_physical_grants.get("INSERT", []) == expected_grants["INSERT"]

        updated_virtual_grants = engine_adapter._get_current_grants_config(
            exp.to_table(virtual_view_name, dialect=engine_adapter.dialect)
        )
        assert set(updated_virtual_grants.get("SELECT", [])) == set(expected_grants["SELECT"])
        assert updated_virtual_grants.get("INSERT", []) == expected_grants["INSERT"]


def _vde_dev_only_config(gateway: str, config: Config) -> None:
    config.virtual_environment_mode = VirtualEnvironmentMode.DEV_ONLY


@pytest.mark.parametrize(
    "grants_target_layer,model_kind",
    [
        ("virtual", "FULL"),
        ("physical", "FULL"),
        ("all", "FULL"),
        ("virtual", "VIEW"),
        ("physical", "VIEW"),
    ],
)
def test_grants_target_layer_with_vde_dev_only(
    engine_adapter: PostgresEngineAdapter,
    ctx: TestContext,
    tmp_path: Path,
    grants_target_layer: str,
    model_kind: str,
):
    with create_users(engine_adapter, "reader", "writer") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)

        if model_kind == "VIEW":
            grants_config = (
                f"'SELECT' = ['{roles['reader']['username']}', '{roles['writer']['username']}']"
            )
        else:
            grants_config = f"""
                'SELECT' = ['{roles["reader"]["username"]}', '{roles["writer"]["username"]}'],
                'INSERT' = ['{roles["writer"]["username"]}']
            """.strip()

        model_def = f"""
        MODEL (
            name test_schema.vde_model_{grants_target_layer}_{model_kind.lower()},
            kind {model_kind},
            grants (
                {grants_config}
            ),
            grants_target_layer '{grants_target_layer}'
        );
        SELECT 1 as id, '{grants_target_layer}_{model_kind}' as test_type
        """
        (
            tmp_path / "models" / f"vde_model_{grants_target_layer}_{model_kind.lower()}.sql"
        ).write_text(model_def)

        context = ctx.create_context(path=tmp_path, config_mutator=_vde_dev_only_config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        table_name = f"test_schema.vde_model_{grants_target_layer}_{model_kind.lower()}"

        # In VDE dev_only mode, VIEWs are created as actual views
        assert context.engine_adapter.table_exists(table_name)

        grants = engine_adapter._get_current_grants_config(
            exp.to_table(table_name, dialect=engine_adapter.dialect)
        )
        assert roles["reader"]["username"] in grants.get("SELECT", [])
        assert roles["writer"]["username"] in grants.get("SELECT", [])

        if model_kind != "VIEW":
            assert roles["writer"]["username"] in grants.get("INSERT", [])


def test_grants_incremental_model_with_vde_dev_only(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, tmp_path: Path
):
    with create_users(engine_adapter, "etl", "analyst") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)

        model_def = f"""
        MODEL (
            name test_schema.vde_incremental_model,
            kind INCREMENTAL_BY_TIME_RANGE (
                time_column event_date
            ),
            grants (
                'SELECT' = ['{roles["analyst"]["username"]}'],
                'INSERT' = ['{roles["etl"]["username"]}']
            ),
            grants_target_layer 'virtual'
        );
        SELECT
            1 as id,
            @start_date::date as event_date,
            'event' as event_type
        """
        (tmp_path / "models" / "vde_incremental_model.sql").write_text(model_def)

        context = ctx.create_context(path=tmp_path, config_mutator=_vde_dev_only_config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        prod_table = "test_schema.vde_incremental_model"
        prod_grants = engine_adapter._get_current_grants_config(
            exp.to_table(prod_table, dialect=engine_adapter.dialect)
        )
        assert roles["analyst"]["username"] in prod_grants.get("SELECT", [])
        assert roles["etl"]["username"] in prod_grants.get("INSERT", [])


@pytest.mark.parametrize(
    "change_type,initial_query,updated_query,expect_schema_change",
    [
        # Metadata-only change (grants only)
        (
            "metadata_only",
            "SELECT 1 as id, 'same' as status",
            "SELECT 1 as id, 'same' as status",
            False,
        ),
        # Breaking change only
        (
            "breaking_only",
            "SELECT 1 as id, 'initial' as status, 100 as amount",
            "SELECT 1 as id, 'updated' as status",  # Removed column
            True,
        ),
        # Both metadata and breaking changes
        (
            "metadata_and_breaking",
            "SELECT 1 as id, 'initial' as status, 100 as amount",
            "SELECT 2 as id, 'changed' as new_status",  # Different schema
            True,
        ),
    ],
)
def test_grants_changes_with_vde_dev_only(
    engine_adapter: PostgresEngineAdapter,
    ctx: TestContext,
    tmp_path: Path,
    change_type: str,
    initial_query: str,
    updated_query: str,
    expect_schema_change: bool,
):
    with create_users(engine_adapter, "user1", "user2", "user3") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)
        model_path = tmp_path / "models" / f"vde_changes_{change_type}.sql"

        initial_model = f"""
        MODEL (
            name test_schema.vde_changes_{change_type},
            kind FULL,
            grants (
                'SELECT' = ['{roles["user1"]["username"]}']
            ),
            grants_target_layer 'virtual'
        );
        {initial_query}
        """
        model_path.write_text(initial_model)

        context = ctx.create_context(path=tmp_path, config_mutator=_vde_dev_only_config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        table_name = f"test_schema.vde_changes_{change_type}"
        initial_grants = engine_adapter._get_current_grants_config(
            exp.to_table(table_name, dialect=engine_adapter.dialect)
        )
        assert roles["user1"]["username"] in initial_grants.get("SELECT", [])
        assert roles["user2"]["username"] not in initial_grants.get("SELECT", [])

        # Update model with new grants and potentially new query
        updated_model = f"""
        MODEL (
            name test_schema.vde_changes_{change_type},
            kind FULL,
            grants (
                'SELECT' = ['{roles["user1"]["username"]}', '{roles["user2"]["username"]}', '{roles["user3"]["username"]}'],
                'INSERT' = ['{roles["user3"]["username"]}']
            ),
            grants_target_layer 'virtual'
        );
        {updated_query}
        """
        model_path.write_text(updated_model)

        # Get initial table columns
        initial_columns = set(
            col[0]
            for col in engine_adapter.fetchall(
                f"SELECT column_name FROM information_schema.columns WHERE table_schema = 'test_schema' AND table_name = 'vde_changes_{change_type}'"
            )
        )

        context.load()
        plan = context.plan("prod", auto_apply=True, no_prompts=True)

        assert len(plan.new_snapshots) == 1

        current_columns = set(
            col[0]
            for col in engine_adapter.fetchall(
                f"SELECT column_name FROM information_schema.columns WHERE table_schema = 'test_schema' AND table_name = 'vde_changes_{change_type}'"
            )
        )

        if expect_schema_change:
            assert current_columns != initial_columns
        else:
            # For metadata-only changes, schema should be the same
            assert current_columns == initial_columns

        # Grants should be updated in all cases
        updated_grants = engine_adapter._get_current_grants_config(
            exp.to_table(table_name, dialect=engine_adapter.dialect)
        )
        assert roles["user1"]["username"] in updated_grants.get("SELECT", [])
        assert roles["user2"]["username"] in updated_grants.get("SELECT", [])
        assert roles["user3"]["username"] in updated_grants.get("SELECT", [])
        assert roles["user3"]["username"] in updated_grants.get("INSERT", [])


@pytest.mark.parametrize(
    "grants_target_layer,environment",
    [
        ("virtual", "prod"),
        ("virtual", "dev"),
        ("physical", "prod"),
        ("physical", "staging"),
        ("all", "prod"),
        ("all", "preview"),
    ],
)
def test_grants_target_layer_plan_env_with_vde_dev_only(
    engine_adapter: PostgresEngineAdapter,
    ctx: TestContext,
    tmp_path: Path,
    grants_target_layer: str,
    environment: str,
):
    with create_users(engine_adapter, "grantee") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)

        model_def = f"""
        MODEL (
            name test_schema.vde_layer_model,
            kind FULL,
            grants (
                'SELECT' = ['{roles["grantee"]["username"]}']
            ),
            grants_target_layer '{grants_target_layer}'
        );
        SELECT 1 as id, '{environment}' as env, '{grants_target_layer}' as layer
        """
        (tmp_path / "models" / "vde_layer_model.sql").write_text(model_def)

        context = ctx.create_context(path=tmp_path, config_mutator=_vde_dev_only_config)

        if environment == "prod":
            context.plan("prod", auto_apply=True, no_prompts=True)
            table_name = "test_schema.vde_layer_model"
            grants = engine_adapter._get_current_grants_config(
                exp.to_table(table_name, dialect=engine_adapter.dialect)
            )
            assert roles["grantee"]["username"] in grants.get("SELECT", [])
        else:
            context.plan(environment, auto_apply=True, no_prompts=True, include_unmodified=True)
            virtual_view = f"test_schema__{environment}.vde_layer_model"
            assert context.engine_adapter.table_exists(virtual_view)
            virtual_grants = engine_adapter._get_current_grants_config(
                exp.to_table(virtual_view, dialect=engine_adapter.dialect)
            )

            data_objects = engine_adapter.get_data_objects("sqlmesh__test_schema")
            physical_tables = [
                obj
                for obj in data_objects
                if "vde_layer_model" in obj.name
                and obj.name.endswith("__dev")  # Always __dev suffix in VDE dev_only
                and "TABLE" in str(obj.type).upper()
            ]

            if grants_target_layer == "virtual":
                # Virtual layer should have grants, physical should not
                assert roles["grantee"]["username"] in virtual_grants.get("SELECT", [])

                assert len(physical_tables) > 0
                for physical_table in physical_tables:
                    physical_table_name = f"sqlmesh__test_schema.{physical_table.name}"
                    physical_grants = engine_adapter._get_current_grants_config(
                        exp.to_table(physical_table_name, dialect=engine_adapter.dialect)
                    )
                    assert roles["grantee"]["username"] not in physical_grants.get("SELECT", [])

            elif grants_target_layer == "physical":
                # Virtual layer should not have grants, physical should
                assert roles["grantee"]["username"] not in virtual_grants.get("SELECT", [])

                assert len(physical_tables) > 0
                for physical_table in physical_tables:
                    physical_table_name = f"sqlmesh__test_schema.{physical_table.name}"
                    physical_grants = engine_adapter._get_current_grants_config(
                        exp.to_table(physical_table_name, dialect=engine_adapter.dialect)
                    )
                    assert roles["grantee"]["username"] in physical_grants.get("SELECT", [])

            else:  # grants_target_layer == "all"
                # Both layers should have grants
                assert roles["grantee"]["username"] in virtual_grants.get("SELECT", [])
                assert len(physical_tables) > 0
                for physical_table in physical_tables:
                    physical_table_name = f"sqlmesh__test_schema.{physical_table.name}"
                    physical_grants = engine_adapter._get_current_grants_config(
                        exp.to_table(physical_table_name, dialect=engine_adapter.dialect)
                    )
                    assert roles["grantee"]["username"] in physical_grants.get("SELECT", [])


@pytest.mark.parametrize(
    "model_kind",
    [
        "SCD_TYPE_2",
        "SCD_TYPE_2_BY_TIME",
    ],
)
def test_grants_plan_scd_type_2_models(
    engine_adapter: PostgresEngineAdapter,
    ctx: TestContext,
    tmp_path: Path,
    model_kind: str,
):
    with create_users(engine_adapter, "reader", "writer", "analyst") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)
        model_name = "scd_model"

        kind_config = f"{model_kind} (unique_key [id])"
        model_definition = f"""
        MODEL (
            name test_schema.{model_name},
            kind {kind_config},
            grants (
                'SELECT' = ['{roles["reader"]["username"]}'],
                'INSERT' = ['{roles["writer"]["username"]}']
            ),
            grants_target_layer 'all'
        );
        SELECT 1 as id, 'initial_data' as name, CURRENT_TIMESTAMP as updated_at
        """
        (tmp_path / "models" / f"{model_name}.sql").write_text(model_definition)

        context = ctx.create_context(path=tmp_path)
        plan_result = context.plan(
            "dev", start="2023-01-01", end="2023-01-01", auto_apply=True, no_prompts=True
        )
        assert len(plan_result.new_snapshots) == 1

        current_snapshot = plan_result.new_snapshots[0]
        fingerprint_version = current_snapshot.fingerprint.to_version()
        physical_table_name = (
            f"sqlmesh__test_schema.test_schema__{model_name}__{fingerprint_version}__dev"
        )
        physical_grants = engine_adapter._get_current_grants_config(
            exp.to_table(physical_table_name, dialect=engine_adapter.dialect)
        )
        assert physical_grants.get("SELECT", []) == [roles["reader"]["username"]]
        assert physical_grants.get("INSERT", []) == [roles["writer"]["username"]]

        view_name = f"test_schema__dev.{model_name}"
        view_grants = engine_adapter._get_current_grants_config(
            exp.to_table(view_name, dialect=engine_adapter.dialect)
        )
        assert view_grants.get("SELECT", []) == [roles["reader"]["username"]]
        assert view_grants.get("INSERT", []) == [roles["writer"]["username"]]

        # Data change
        updated_model_definition = f"""
        MODEL (
            name test_schema.{model_name},
            kind {kind_config},
            grants (
                'SELECT' = ['{roles["reader"]["username"]}'],
                'INSERT' = ['{roles["writer"]["username"]}']
            ),
            grants_target_layer 'all'
        );
        SELECT 1 as id, 'updated_data' as name, CURRENT_TIMESTAMP as updated_at
        """
        (tmp_path / "models" / f"{model_name}.sql").write_text(updated_model_definition)

        context.load()
        context.plan("dev", start="2023-01-02", end="2023-01-02", auto_apply=True, no_prompts=True)

        snapshot = context.get_snapshot(f"test_schema.{model_name}")
        assert snapshot
        fingerprint = snapshot.fingerprint.to_version()
        table_name = f"sqlmesh__test_schema.test_schema__{model_name}__{fingerprint}__dev"
        data_change_grants = engine_adapter._get_current_grants_config(
            exp.to_table(table_name, dialect=engine_adapter.dialect)
        )
        assert data_change_grants.get("SELECT", []) == [roles["reader"]["username"]]
        assert data_change_grants.get("INSERT", []) == [roles["writer"]["username"]]

        # Data + grants changes
        grant_change_model_definition = f"""
        MODEL (
            name test_schema.{model_name},
            kind {kind_config},
            grants (
                'SELECT' = ['{roles["reader"]["username"]}', '{roles["analyst"]["username"]}'],
                'INSERT' = ['{roles["writer"]["username"]}'],
                'UPDATE' = ['{roles["analyst"]["username"]}']
            ),
            grants_target_layer 'all'
        );
        SELECT 1 as id, 'grant_changed_data' as name, CURRENT_TIMESTAMP as updated_at
        """
        (tmp_path / "models" / f"{model_name}.sql").write_text(grant_change_model_definition)

        context.load()
        context.plan("dev", start="2023-01-03", end="2023-01-03", auto_apply=True, no_prompts=True)

        snapshot = context.get_snapshot(f"test_schema.{model_name}")
        assert snapshot
        fingerprint = snapshot.fingerprint.to_version()
        table_name = f"sqlmesh__test_schema.test_schema__{model_name}__{fingerprint}__dev"
        final_grants = engine_adapter._get_current_grants_config(
            exp.to_table(table_name, dialect=engine_adapter.dialect)
        )
        expected_select_users = {roles["reader"]["username"], roles["analyst"]["username"]}
        assert set(final_grants.get("SELECT", [])) == expected_select_users
        assert final_grants.get("INSERT", []) == [roles["writer"]["username"]]
        assert final_grants.get("UPDATE", []) == [roles["analyst"]["username"]]

        final_view_grants = engine_adapter._get_current_grants_config(
            exp.to_table(view_name, dialect=engine_adapter.dialect)
        )
        assert set(final_view_grants.get("SELECT", [])) == expected_select_users
        assert final_view_grants.get("INSERT", []) == [roles["writer"]["username"]]
        assert final_view_grants.get("UPDATE", []) == [roles["analyst"]["username"]]


@pytest.mark.parametrize(
    "model_kind",
    [
        "SCD_TYPE_2",
        "SCD_TYPE_2_BY_TIME",
    ],
)
def test_grants_plan_scd_type_2_with_vde_dev_only(
    engine_adapter: PostgresEngineAdapter,
    ctx: TestContext,
    tmp_path: Path,
    model_kind: str,
):
    with create_users(engine_adapter, "etl_user", "analyst") as roles:
        (tmp_path / "models").mkdir(exist_ok=True)
        model_name = "vde_scd_model"

        model_def = f"""
        MODEL (
            name test_schema.{model_name},
            kind {model_kind} (unique_key [customer_id]),
            grants (
                'SELECT' = ['{roles["analyst"]["username"]}'],
                'INSERT' = ['{roles["etl_user"]["username"]}']
            ),
            grants_target_layer 'all'
        );
        SELECT
            1 as customer_id,
            'active' as status,
            CURRENT_TIMESTAMP as updated_at
        """
        (tmp_path / "models" / f"{model_name}.sql").write_text(model_def)

        context = ctx.create_context(path=tmp_path, config_mutator=_vde_dev_only_config)

        # Prod
        context.plan("prod", auto_apply=True, no_prompts=True)
        prod_table = f"test_schema.{model_name}"
        prod_grants = engine_adapter._get_current_grants_config(
            exp.to_table(prod_table, dialect=engine_adapter.dialect)
        )
        assert roles["analyst"]["username"] in prod_grants.get("SELECT", [])
        assert roles["etl_user"]["username"] in prod_grants.get("INSERT", [])

        # Dev
        context.plan("dev", auto_apply=True, no_prompts=True, include_unmodified=True)
        dev_view = f"test_schema__dev.{model_name}"
        dev_grants = engine_adapter._get_current_grants_config(
            exp.to_table(dev_view, dialect=engine_adapter.dialect)
        )
        assert roles["analyst"]["username"] in dev_grants.get("SELECT", [])
        assert roles["etl_user"]["username"] in dev_grants.get("INSERT", [])

        snapshot = context.get_snapshot(f"test_schema.{model_name}")
        assert snapshot
        fingerprint_version = snapshot.fingerprint.to_version()
        dev_physical_table_name = (
            f"sqlmesh__test_schema.test_schema__{model_name}__{fingerprint_version}__dev"
        )

        dev_physical_grants = engine_adapter._get_current_grants_config(
            exp.to_table(dev_physical_table_name, dialect=engine_adapter.dialect)
        )
        assert roles["analyst"]["username"] in dev_physical_grants.get("SELECT", [])
        assert roles["etl_user"]["username"] in dev_physical_grants.get("INSERT", [])


# --------------------------------------------------------------------------------------------
# DataFrame bulk loading via COPY
# --------------------------------------------------------------------------------------------


def _ts(values: t.Any) -> pd.DatetimeIndex:
    return pd.to_datetime(values, format="ISO8601")


def _types(**columns: str) -> t.Dict[str, exp.DataType]:
    return {name: exp.DataType.build(kind, dialect="postgres") for name, kind in columns.items()}


def _leftover_staging_tables(
    engine_adapter: PostgresEngineAdapter, schema_like: str
) -> t.List[str]:
    rows = engine_adapter.fetchall(
        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
        f"WHERE table_name LIKE '\\_\\_temp\\_%' AND table_schema LIKE '%{schema_like}'"
    )
    return [r[0] for r in rows]


@contextmanager
def _values_path(adapter: PostgresEngineAdapter) -> t.Iterator[None]:
    """Forces DataFrames through the generic `VALUES (...)` implementation."""
    with patch.object(
        PostgresEngineAdapter, "_df_to_source_queries", EngineAdapter._df_to_source_queries
    ):
        yield


def _assert_same_rows(adapter: PostgresEngineAdapter, a: exp.Table, b: exp.Table) -> None:
    a_sql, b_sql = a.sql("postgres", identify=True), b.sql("postgres", identify=True)
    (a_count,) = adapter.fetchone(f"SELECT COUNT(*) FROM {a_sql}")  # type: ignore
    (b_count,) = adapter.fetchone(f"SELECT COUNT(*) FROM {b_sql}")  # type: ignore
    assert a_count == b_count > 0
    for left, right in ((a_sql, b_sql), (b_sql, a_sql)):
        extra = adapter.fetchall(f"SELECT * FROM {left} EXCEPT ALL SELECT * FROM {right}")
        assert extra == [], f"rows only in {left}: {extra}"


_ALL_THE_STRINGS = [
    "",
    None,
    "\\N",
    "NULL",
    "null",
    "\\",
    "\\\\",
    "\\x41",
    'a"b',
    '"',
    "a,b",
    "a\nb",
    "a\rb",
    "a\r\nb",
    "a\tb",
    " padded ",
    "h\u00e9llo w\u00f6rld",
    "\u65e5\u672c\u8a9e",
    "\U0001f600 emoji",
    "it's",
    "\\.",
    "end\\.",
    "\\.\nafter",
    "a" * 5000,
]

_TS_NAIVE = _ts(
    [
        "2020-01-01 00:00:00",
        "2020-06-15 12:34:56.123456",
        "1969-12-31 23:59:59.999999",
        "1900-01-01 00:00:00",
        "1677-09-22 00:00:00",
        "2262-04-11 23:47:16.854775",
        "2024-02-29 00:00:00.000001",
    ]
)

# the extreme values can't be converted to every time zone
_TS_TZ = _TS_NAIVE[[0, 1, 2, 3, 6]]

# (id, DataFrame, target types, source_columns, expect the COPY path to be used)
_PARITY_CASES: t.List[t.Any] = [
    pytest.param(
        pd.DataFrame({"s": _ALL_THE_STRINGS, "i": range(len(_ALL_THE_STRINGS))}),
        _types(s="text", i="int"),
        None,
        True,
        id="strings-multi-column",
    ),
    pytest.param(
        pd.DataFrame({"s": [x for x in _ALL_THE_STRINGS if x != "\\."]}),
        _types(s="text"),
        None,
        True,
        id="strings-single-column",
    ),
    pytest.param(
        pd.DataFrame({"s": ["a", "\\.", "b"]}),
        _types(s="text"),
        None,
        False,
        id="strings-end-of-copy-marker-falls-back",
    ),
    pytest.param(
        pd.DataFrame({"s": ["", "x", None, ""]}),
        _types(s="varchar(10)"),
        None,
        True,
        id="strings-single-column-empty-and-null",
    ),
    pytest.param(
        pd.DataFrame({"i": [1, None, 3, None], "j": [5, 6, 7, 8]}),
        _types(i="int", j="smallint"),
        None,
        True,
        id="int-with-nulls-is-float-in-pandas",
    ),
    pytest.param(
        pd.DataFrame(
            {
                "i": [2**63 - 1, -(2**63), 9007199254740993, 0, -1],
                "j": [1, 2, 3, 4, 5],
            }
        ),
        _types(i="bigint", j="int"),
        None,
        True,
        id="int64-bounds-and-2^53+1",
    ),
    pytest.param(
        pd.DataFrame(
            {"i": np.array([1, -2, 3], dtype="int8"), "j": np.array([4, 5, 6], dtype="uint32")}
        ),
        _types(i="smallint", j="bigint"),
        None,
        True,
        id="small-int-dtypes",
    ),
    pytest.param(
        pd.DataFrame({"f": [1.5, 2.5, 0.5, -0.5, -1.5, -2.5, 3.4999999999, 1e10]}),
        _types(f="bigint"),
        None,
        True,
        id="float-into-bigint-rounds-like-values",
    ),
    pytest.param(
        pd.DataFrame(
            {
                "f": [
                    0.1,
                    1e-300,
                    1e300,
                    0.30000000000000004,
                    -0.0,
                    5e-324,
                    1.7976931348623157e308,
                    123456789.123456789,
                    np.nan,
                ]
            }
        ),
        _types(f="double precision"),
        None,
        True,
        id="float64-into-double",
    ),
    pytest.param(
        pd.DataFrame(
            {"f": [0.1, 0.30000000000000004, 16777217.0, 1e-20, 123456789.123456789, -2.5, np.nan]}
        ),
        _types(f="real"),
        None,
        True,
        id="float64-into-real",
    ),
    pytest.param(
        pd.DataFrame(
            {"f": [0.1, 0.30000000000000004, 1e-12, 123456789.123456789, -2.5, 1e10, np.nan]}
        ),
        _types(f="numeric(38,20)"),
        None,
        True,
        id="float64-into-numeric-high-scale",
    ),
    pytest.param(
        pd.DataFrame({"b": [True, False, True], "c": [True, None, False]}),
        _types(b="boolean", c="boolean"),
        None,
        True,
        id="bool-and-bool-with-nulls",
    ),
    pytest.param(
        pd.DataFrame({"ts": _TS_NAIVE, "tstz": _TS_NAIVE}),
        _types(ts="timestamp", tstz="timestamptz"),
        None,
        True,
        id="naive-timestamps",
    ),
    pytest.param(
        pd.DataFrame(
            {
                "utc": _TS_TZ.tz_localize("UTC"),
                "east": _TS_TZ.tz_localize("UTC").tz_convert("US/Eastern"),
                "kolkata": _TS_TZ.tz_localize("UTC").tz_convert("Asia/Kolkata"),
            }
        ),
        _types(utc="timestamptz", east="timestamp", kolkata="timestamptz"),
        None,
        True,
        id="tz-aware-timestamps",
    ),
    pytest.param(
        pd.DataFrame({"ts": _ts(["2020-01-01", "2020-01-02", "2020-01-03"])}),
        _types(ts="timestamp"),
        None,
        True,
        id="midnight-only-timestamps",
    ),
    pytest.param(
        pd.DataFrame({"d": ["2020-01-01", "1969-12-31", "2024-02-29", None]}),
        _types(d="date"),
        None,
        True,
        id="date-from-strings",
    ),
    pytest.param(
        pd.DataFrame({"n": ["1.5", "-0.000000001", "12345678901234567890.123456789", "0", None]}),
        _types(n="numeric(38,9)"),
        None,
        True,
        id="numeric-from-strings",
    ),
    pytest.param(
        pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"], "ignored": [0, 0, 0]}),
        _types(a="int", b="text", c="int"),
        ["a", "b"],
        True,
        id="source-columns-subset-with-missing-target-column",
    ),
    pytest.param(
        pd.DataFrame(
            {
                "Order": [1, 2],
                "ID": ["a", "b"],
                "select": [0.5, 1.5],
                "with space": [True, False],
                'we"ird': ["x", "y"],
                "\u00fcber": [1, 2],
            }
        ),
        _types(
            **{
                "Order": "int",
                "ID": "text",
                "select": "double precision",
                "with space": "boolean",
                'we"ird': "text",
                "\u00fcber": "int",
            }
        ),
        None,
        True,
        id="reserved-mixed-case-and-unicode-column-names",
    ),
    pytest.param(
        pd.DataFrame(
            {
                "f": pd.Series([1.5, None, 0.1, 2.5], dtype=object),
                "i": pd.Series([1, None, 3, 2**70], dtype=object),
                "m": pd.Series([1, 2.5, None, 3], dtype=object),
                "b": pd.Series([True, None, False, True], dtype=object),
            }
        ),
        _types(f="double precision", i="numeric(30,0)", m="numeric(10,2)", b="boolean"),
        None,
        True,
        id="object-columns-with-python-numbers-and-none",
    ),
    pytest.param(
        pd.DataFrame({"s": [None, None], "i": [None, None]}),
        _types(s="text", i="int"),
        None,
        True,
        id="all-null-columns",
    ),
]


@pytest.mark.parametrize("df, types, source_columns, expect_copy", _PARITY_CASES)
@pytest.mark.parametrize("chunk_size", [10000, 3])
def test_df_copy_matches_values_path(
    engine_adapter: PostgresEngineAdapter,
    ctx: TestContext,
    mocker: MockerFixture,
    df: pd.DataFrame,
    types: t.Dict[str, exp.DataType],
    source_columns: t.Optional[t.List[str]],
    expect_copy: bool,
    chunk_size: int,
):
    """Differential test: COPY and the generic VALUES implementation must produce identical rows."""
    copy_table = ctx.table("parity_copy")
    values_table = ctx.table("parity_values")
    for table in (copy_table, values_table):
        engine_adapter.create_table(table, types)

    engine_adapter.DEFAULT_BATCH_SIZE = chunk_size
    spy = mocker.spy(PostgresEngineAdapter, "_copy_df")
    engine_adapter.insert_append(
        copy_table, df, target_columns_to_types=types, source_columns=source_columns
    )
    assert (spy.call_count > 0) is expect_copy

    with _values_path(engine_adapter):
        engine_adapter.insert_append(
            values_table, df, target_columns_to_types=types, source_columns=source_columns
        )

    _assert_same_rows(engine_adapter, copy_table, values_table)
    assert (
        _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name) == []
    )


def test_df_copy_exact_values(engine_adapter: PostgresEngineAdapter, ctx: TestContext):
    """Guards against the parity test passing because both paths are equally wrong."""
    table = ctx.table("exact")
    types = _types(
        id="int",
        s="text",
        i="int",
        f="double precision",
        b="boolean",
        ts="timestamptz",
        n="numeric(38,9)",
    )
    engine_adapter.create_table(table, types)
    df = pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "s": ["", None, "\\N", 'a"b,c\nd'],
            "i": [1, None, 3, 4],
            "f": [0.1, np.nan, 1e-300, 2.5],
            "b": [True, None, False, True],
            "ts": _ts(["2020-01-01 00:00:00.123456", None, "1960-01-01", "2020-01-02"]).tz_localize(
                "UTC"
            ),
            "n": ["1.5", None, "2", "-0.000000001"],
        }
    )
    engine_adapter.insert_append(table, df, target_columns_to_types=types)

    rows = engine_adapter.fetchall(
        f"SELECT id, s, i, f, b, to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US'), n::text "
        f"FROM {table.sql('postgres', identify=True)} ORDER BY id"
    )
    assert rows == [
        (1, "", 1, 0.1, True, "2020-01-01 00:00:00.123456", "1.500000000"),
        (2, None, None, None, None, None, None),
        (3, "\\N", 3, 1e-300, False, "1960-01-01 00:00:00.000000", "2.000000000"),
        (4, 'a"b,c\nd', 4, 2.5, True, "2020-01-02 00:00:00.000000", "-0.000000001"),
    ]


def test_df_copy_nullable_pandas_dtypes(engine_adapter: PostgresEngineAdapter, ctx: TestContext):
    """The generic implementation can't load pd.NA at all, COPY handles the nullable dtypes."""
    table = ctx.table("nullable")
    types = _types(i="int", j="bigint", f="double precision", b="boolean", s="text")
    engine_adapter.create_table(table, types)
    df = pd.DataFrame(
        {
            "i": pd.array([1, None, 3], dtype="Int64"),
            "j": pd.array([2**62, -(2**62), None], dtype="Int64"),
            "f": pd.array([0.1, None, 2.5], dtype="Float64"),
            "b": pd.array([True, None, False], dtype="boolean"),
            "s": pd.array(["x", None, ""], dtype="string"),
        }
    )
    engine_adapter.insert_append(table, df, target_columns_to_types=types)
    rows = engine_adapter.fetchall(
        f"SELECT * FROM {table.sql('postgres', identify=True)} ORDER BY i NULLS LAST"
    )
    assert rows == [
        (1, 2**62, 0.1, True, "x"),
        (3, None, 2.5, False, ""),
        (None, -(2**62), None, None, None),
    ]


def test_df_copy_source_columns_in_a_different_order(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext
):
    table = ctx.table("reordered")
    types = _types(a="int", b="text", c="int")
    engine_adapter.create_table(table, types)
    df = pd.DataFrame({"b": ["x", "y"], "a": [1, 2], "ignored": [0, 0]})
    engine_adapter.insert_append(
        table, df, target_columns_to_types=types, source_columns=["b", "a"]
    )
    assert engine_adapter.fetchall(
        f"SELECT a, b, c FROM {table.sql('postgres', identify=True)} ORDER BY a"
    ) == [(1, "x", None), (2, "y", None)]


def test_df_copy_infinity(engine_adapter: PostgresEngineAdapter, ctx: TestContext):
    if engine_adapter.server_version < (14, 0):
        pytest.skip("NUMERIC infinity requires PostgreSQL 14+")
    table = ctx.table("inf")
    types = _types(f="double precision")
    engine_adapter.create_table(table, types)
    df = pd.DataFrame({"f": [float("inf"), float("-inf"), 1.0]})
    engine_adapter.insert_append(table, df, target_columns_to_types=types)
    rows = engine_adapter.fetchall(
        f"SELECT f FROM {table.sql('postgres', identify=True)} ORDER BY f"
    )
    assert rows == [(float("-inf"),), (1.0,), (float("inf"),)]


def test_df_copy_null_timestamps(engine_adapter: PostgresEngineAdapter, ctx: TestContext):
    table = ctx.table("nat")
    types = _types(ts="timestamp", tstz="timestamptz")
    engine_adapter.create_table(table, types)
    df = pd.DataFrame(
        {
            "ts": _ts(["2020-01-01", None]),
            "tstz": _ts(["2020-01-01", None]).tz_localize("UTC"),
        }
    )
    engine_adapter.insert_append(table, df, target_columns_to_types=types)
    rows = engine_adapter.fetchall(
        f"SELECT ts, tstz FROM {table.sql('postgres', identify=True)} ORDER BY ts NULLS LAST"
    )
    assert rows[1] == (None, None)
    assert rows[0][0] is not None and rows[0][1] is not None


def test_df_copy_in_non_utf8_client_encoding(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext
):
    connection = engine_adapter._connection_pool.get()
    connection.set_client_encoding("LATIN1")
    try:
        table = ctx.table("latin1")
        types = _types(s="text")
        engine_adapter.create_table(table, types)
        values = ["h\u00e9llo", "caf\u00e9 \u00fcber", "plain"]
        engine_adapter.insert_append(
            table, pd.DataFrame({"s": values}), target_columns_to_types=types
        )
        rows = engine_adapter.fetchall(
            f"SELECT s FROM {table.sql('postgres', identify=True)} ORDER BY s"
        )
        assert sorted(r[0] for r in rows) == sorted(values)
    finally:
        connection.set_client_encoding("UTF8")


def test_df_copy_cleans_up_staging_table_on_failed_insert(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, mocker: MockerFixture
):
    table = ctx.table("not_null_target")
    engine_adapter.execute(
        f"CREATE TABLE {table.sql('postgres', identify=True)} (a INT NOT NULL, b TEXT)"
    )
    spy = mocker.spy(PostgresEngineAdapter, "_copy_df")
    df = pd.DataFrame({"a": [1, None], "b": ["x", "y"]})

    with pytest.raises(Exception, match='null value in column "a"'):
        engine_adapter.insert_append(table, df, target_columns_to_types=_types(a="int", b="text"))

    assert spy.call_count == 1
    assert (
        _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name) == []
    )
    assert engine_adapter.fetchall(f"SELECT * FROM {table.sql('postgres', identify=True)}") == []
    # The connection must still be usable
    assert engine_adapter.fetchone("select 1") == (1,)


def test_df_copy_cleans_up_staging_table_outside_transaction(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext
):
    table = ctx.table("outside_txn")
    types = _types(a="int")
    engine_adapter.create_table(table, types)
    source_queries = engine_adapter._df_to_source_queries(
        pd.DataFrame({"a": [1, 2]}), types, 10000, table
    )
    with source_queries[0] as query:
        assert (
            len(
                _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name)
            )
            == 1
        )
        assert engine_adapter.fetchall(query) == [(1,), (2,)]
    assert (
        _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name) == []
    )


def test_df_copy_long_physical_table_name(engine_adapter: PostgresEngineAdapter, ctx: TestContext):
    name = (
        "sqlmesh_physical_" + "x" * 30 + "__" + "9" * 12
    )  # 61 characters, a typical physical name
    table = ctx.table(name)
    assert len(table.name) > 47
    types = _types(a="int")
    engine_adapter.create_table(table, types)
    engine_adapter.insert_append(
        table, pd.DataFrame({"a": [1, 2, 3]}), target_columns_to_types=types
    )
    assert engine_adapter.fetchall(
        f"SELECT a FROM {table.sql('postgres', identify=True)} ORDER BY a"
    ) == [(1,), (2,), (3,)]
    assert (
        _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name) == []
    )


def test_df_copy_concurrent_threads(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, config: Config
):
    """Every thread has its own connection (and transaction); staging names must not collide."""
    connection_config = config.gateways[ctx.gateway].connection
    assert connection_config
    # `concurrent_tasks > 1` gives every thread its own connection, like during a plan
    adapter = t.cast(
        PostgresEngineAdapter, connection_config.create_engine_adapter(concurrent_tasks=6)
    )
    types = _types(a="int", b="text")
    tables = [ctx.table(f"concurrent_{i}") for i in range(6)]
    for table in tables:
        engine_adapter.create_table(table, types)

    adapter.DEFAULT_BATCH_SIZE = 300

    def _load(i: int) -> None:
        df = pd.DataFrame({"a": range(i * 1000, i * 1000 + 1000), "b": [f"t{i}"] * 1000})
        adapter.insert_append(tables[i], df, target_columns_to_types=types)

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(_load, range(6)))
    finally:
        adapter.close()

    for i, table in enumerate(tables):
        rows = engine_adapter.fetchall(
            f"SELECT MIN(a), MAX(a), COUNT(*), COUNT(DISTINCT b), MIN(b) FROM {table.sql('postgres', identify=True)}"
        )
        assert rows == [(i * 1000, i * 1000 + 999, 1000, 1, f"t{i}")]
    assert (
        _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name) == []
    )


def test_df_copy_without_create_privilege_fails_like_other_ddl(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, config: Config
):
    schema = exp.to_table(ctx.schema("no_create")).name
    engine_adapter.create_schema(schema)
    table = exp.to_table(f"{schema}.target")
    types = _types(a="int")
    engine_adapter.create_table(table, types)
    with create_users(engine_adapter, "reader") as roles:
        user = roles["reader"]["username"]
        engine_adapter.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{user}"')
        engine_adapter.execute(f'GRANT INSERT, SELECT ON "{schema}"."target" TO "{user}"')
        reader = create_engine_adapter_for_role(roles["reader"], ctx, config)
        try:
            with pytest.raises(Exception, match="permission denied for schema"):
                reader.insert_append(table, pd.DataFrame({"a": [1]}), target_columns_to_types=types)
            assert reader.fetchone("select 1") == (1,)
        finally:
            reader.close()


def test_df_copy_generated_and_identity_columns(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext
):
    """Only the listed columns are copied, so identity / generated columns keep working."""
    table = ctx.table("generated")
    sql_table = table.sql("postgres", identify=True)
    engine_adapter.execute(
        f"CREATE TABLE {sql_table} ("
        "id INT GENERATED ALWAYS AS IDENTITY, a INT, doubled INT GENERATED ALWAYS AS (a * 2) STORED, b TEXT)"
    )
    engine_adapter.insert_append(
        table,
        pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}),
        target_columns_to_types=_types(a="int", b="text"),
    )
    assert engine_adapter.fetchall(f"SELECT id, a, doubled, b FROM {sql_table} ORDER BY a") == [
        (1, 1, 2, "x"),
        (2, 2, 4, "y"),
    ]


def test_df_copy_engine_adapter_operations(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, mocker: MockerFixture
):
    spy = mocker.spy(PostgresEngineAdapter, "_copy_df")
    types = _types(id="int", name="text", ds="text")

    def _read(table: exp.Table) -> t.List[t.Tuple]:
        return engine_adapter.fetchall(
            f"SELECT * FROM {table.sql('postgres', identify=True)} ORDER BY id"
        )

    # replace_query: creates the table, then replaces its content
    replaced = ctx.table("replaced")
    engine_adapter.replace_query(
        replaced,
        pd.DataFrame({"id": [1, 2], "name": ["a", "b"], "ds": ["2020-01-01"] * 2}),
        target_columns_to_types=types,
    )
    assert _read(replaced) == [(1, "a", "2020-01-01"), (2, "b", "2020-01-01")]
    engine_adapter.replace_query(
        replaced,
        pd.DataFrame({"id": [3], "name": ["c"], "ds": ["2020-01-02"]}),
        target_columns_to_types=types,
    )
    assert _read(replaced) == [(3, "c", "2020-01-02")]

    # insert_append
    engine_adapter.insert_append(
        replaced,
        pd.DataFrame({"id": [4], "name": [None], "ds": ["2020-01-03"]}),
        target_columns_to_types=types,
    )
    assert _read(replaced) == [(3, "c", "2020-01-02"), (4, None, "2020-01-03")]

    # ctas from a DataFrame
    ctas = ctx.table("ctas")
    engine_adapter.ctas(
        ctas,
        pd.DataFrame({"id": [7], "name": ["z"], "ds": ["2020-01-07"]}),
        target_columns_to_types=types,
    )
    assert _read(ctas) == [(7, "z", "2020-01-07")]

    # insert_overwrite_by_time_partition
    partitioned = ctx.table("partitioned")
    engine_adapter.create_table(partitioned, types)
    engine_adapter.insert_append(
        partitioned,
        pd.DataFrame(
            {"id": [1, 2, 3], "name": list("abc"), "ds": ["2020-01-01", "2020-01-02", "2020-01-03"]}
        ),
        target_columns_to_types=types,
    )
    engine_adapter.insert_overwrite_by_time_partition(
        partitioned,
        pd.DataFrame({"id": [20, 30], "name": ["B", "C"], "ds": ["2020-01-02", "2020-01-03"]}),
        start="2020-01-02",
        end="2020-01-03",
        time_formatter=lambda x, _: exp.Literal.string(to_ds(x)),
        time_column="ds",
        target_columns_to_types=types,
    )
    assert _read(partitioned) == [
        (1, "a", "2020-01-01"),
        (20, "B", "2020-01-02"),
        (30, "C", "2020-01-03"),
    ]

    # merge: native (PG >= 15) and the logical implementation used for PG < 15
    for version in (engine_adapter.server_version, (14, 0)):
        merged = ctx.table(f"merged_{version[0]}")
        engine_adapter.create_table(merged, types)
        engine_adapter.insert_append(
            merged,
            pd.DataFrame({"id": [1, 2], "name": ["a", "b"], "ds": ["x", "x"]}),
            target_columns_to_types=types,
        )
        engine_adapter.server_version = version  # type: ignore
        engine_adapter.merge(
            merged,
            pd.DataFrame({"id": [2, 3], "name": ["B", "C"], "ds": ["y", "y"]}),
            target_columns_to_types=types,
            unique_key=[exp.to_column("id", quoted=True)],
        )
        assert _read(merged) == [(1, "a", "x"), (2, "B", "y"), (3, "C", "y")]

    assert spy.call_count >= 8
    assert (
        _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name) == []
    )


def test_df_copy_python_and_seed_models(ctx: TestContext, tmp_path: Path, mocker: MockerFixture):
    engine_adapter = t.cast(PostgresEngineAdapter, ctx.engine_adapter)
    spy = mocker.spy(PostgresEngineAdapter, "_copy_df")
    schema = exp.to_table(ctx.schema(TEST_SCHEMA)).this
    models_dir = tmp_path / "models"
    seeds_dir = tmp_path / "seeds"
    models_dir.mkdir()
    seeds_dir.mkdir()

    (seeds_dir / "seed.csv").write_text('id,name,score\n1,alice,1.5\n2,"b,ob",\n3,\\N,3.25\n')
    (models_dir / "seed_model.sql").write_text(f"""
    MODEL (
        name {schema}.seed_model,
        kind SEED (path '../seeds/seed.csv'),
        columns (id int, name text, score double)
    );
    """)
    (models_dir / "py_model.py").write_text(f"""
import typing as t
from datetime import datetime

import pandas as pd
from sqlmesh import ExecutionContext, model
from sqlmesh.core.model.kind import ModelKindName


@model(
    "{schema}.py_model",
    kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE, time_column="ds", batch_size=1),
    columns={{"id": "int", "name": "text", "value": "double", "ds": "text"}},
    start="2020-01-01",
    cron="@daily",
)
def execute(context: ExecutionContext, start: datetime, end: datetime, execution_time: datetime, **kwargs: t.Any) -> pd.DataFrame:
    ds = start.strftime("%Y-%m-%d")
    return pd.DataFrame(
        {{
            "id": [1, 2, 3],
            "name": [f"n{{ds}}", None, 'q"uote,'],
            "value": [0.1, float("nan"), 2.5],
            "ds": [ds] * 3,
        }}
    )
""")

    sqlmesh = ctx.create_context(path=tmp_path)
    with time_machine.travel("2020-01-04 00:00:00"):
        sqlmesh.plan(auto_apply=True, no_prompts=True)

    py_rows = engine_adapter.fetchall(
        f'SELECT ds, COUNT(*), COUNT(name), COUNT(value), MIN(id) FROM "{schema}"."py_model" GROUP BY ds ORDER BY ds'
    )
    assert py_rows == [(f"2020-01-0{d}", 3, 2, 2, 1) for d in (1, 2, 3)]
    assert engine_adapter.fetchall(
        f'SELECT id, name, score FROM "{schema}"."seed_model" ORDER BY id'
    ) == [
        (1, "alice", 1.5),
        (2, "b,ob", None),
        (3, "\\N", 3.25),
    ]
    assert spy.call_count >= 4
    assert (
        _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name) == []
    )


@pytest.mark.parametrize("operation", ["replace_query", "insert_append"])
def test_df_copy_failure_leaves_no_staging_table(
    engine_adapter: PostgresEngineAdapter, ctx: TestContext, operation: str
):
    """A failing COPY (here: a NUL character) must not leak the staging table, outside a transaction too."""
    table = ctx.table("copy_failure")
    types = _types(x="text", y="int")
    engine_adapter.create_table(table, types)
    df = pd.DataFrame({"x": ["a\x00b"], "y": [1]})

    with pytest.raises(Exception, match="0x00|null character|NUL"):
        getattr(engine_adapter, operation)(table, df, target_columns_to_types=types)

    assert (
        _leftover_staging_tables(engine_adapter, exp.to_table(ctx.schema(TEST_SCHEMA)).name) == []
    )
    assert engine_adapter.fetchone("select 1") == (1,)
