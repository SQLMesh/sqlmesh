import typing as t
import uuid
from contextlib import contextmanager
import pytest
from pytest import FixtureRequest
from pathlib import Path
from sqlmesh.core.engine_adapter import PostgresEngineAdapter
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
