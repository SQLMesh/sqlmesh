from __future__ import annotations

import typing as t
from unittest.mock import patch
import pytest
from pathlib import Path
from sqlmesh.core.config.naming import NameInferenceConfig
from sqlmesh.core.model.common import ParsableSql
import time_machine
from pytest_mock.plugin import MockerFixture

from sqlmesh.core.config import (
    Config,
    GatewayConfig,
    ModelDefaultsConfig,
    DuckDBConnectionConfig,
)
from sqlmesh.core.config.janitor import JanitorConfig
from sqlmesh.core.context import Context
from sqlmesh.core.model import (
    SqlModel,
)
from sqlmesh.utils.errors import (
    SQLMeshError,
)
from sqlmesh.utils.date import now
from tests.conftest import DuckDBMetadata
from tests.utils.test_helpers import use_terminal_console
from tests.utils.test_filesystem import create_temp_file
from tests.core.integration.utils import add_projection_to_model, apply_to_environment

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_table_name(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    snapshot = context.get_snapshot("sushi.waiter_revenue_by_day")
    assert snapshot
    assert (
        context.table_name("sushi.waiter_revenue_by_day", "prod")
        == f"memory.sqlmesh__sushi.sushi__waiter_revenue_by_day__{snapshot.version}"
    )

    with pytest.raises(SQLMeshError, match="Environment 'dev' was not found."):
        context.table_name("sushi.waiter_revenue_by_day", "dev")

    with pytest.raises(
        SQLMeshError, match="Model 'sushi.missing' was not found in environment 'prod'."
    ):
        context.table_name("sushi.missing", "prod")

    # Add a new projection
    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    context.plan("dev_a", auto_apply=True, no_prompts=True, skip_tests=True)

    new_snapshot = context.get_snapshot("sushi.waiter_revenue_by_day")
    assert new_snapshot.version != snapshot.version

    assert (
        context.table_name("sushi.waiter_revenue_by_day", "dev_a")
        == f"memory.sqlmesh__sushi.sushi__waiter_revenue_by_day__{new_snapshot.version}"
    )

    # Make a forward-only change
    context.upsert_model(model, stamp="forward_only")

    context.plan("dev_b", auto_apply=True, no_prompts=True, skip_tests=True, forward_only=True)

    forward_only_snapshot = context.get_snapshot("sushi.waiter_revenue_by_day")
    assert forward_only_snapshot.version == snapshot.version
    assert forward_only_snapshot.dev_version != snapshot.version

    assert (
        context.table_name("sushi.waiter_revenue_by_day", "dev_b")
        == f"memory.sqlmesh__sushi.sushi__waiter_revenue_by_day__{forward_only_snapshot.dev_version}__dev"
    )

    assert (
        context.table_name("sushi.waiter_revenue_by_day", "dev_b", prod=True)
        == f"memory.sqlmesh__sushi.sushi__waiter_revenue_by_day__{snapshot.version}"
    )


def test_janitor_cleanup_order(mocker: MockerFixture, tmp_path: Path):
    def setup_scenario():
        models_dir = tmp_path / "models"

        if not models_dir.exists():
            models_dir.mkdir()

        model1_path = models_dir / "model1.sql"

        with open(model1_path, "w") as f:
            f.write("MODEL(name test.model1, kind FULL); SELECT 1 AS col")

        config = Config(
            model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        )
        ctx = Context(paths=[tmp_path], config=config)

        ctx.plan("dev", no_prompts=True, auto_apply=True)

        model1_snapshot = ctx.get_snapshot("test.model1")

        # Delete the model file to cause a snapshot expiration
        model1_path.unlink()

        ctx.load()

        ctx.plan("dev", no_prompts=True, auto_apply=True)

        # Invalidate the environment to cause an environment cleanup
        ctx.invalidate_environment("dev")

        try:
            ctx._run_janitor(ignore_ttl=True)
        except:
            pass

        return ctx, model1_snapshot

    # Case 1: Assume that the snapshot cleanup yields an error, the snapshot records
    # should still exist in the state sync so the next janitor can retry
    mocker.patch(
        "sqlmesh.core.snapshot.evaluator.SnapshotEvaluator.cleanup",
        side_effect=Exception("snapshot cleanup error"),
    )
    ctx, model1_snapshot = setup_scenario()

    # - Check that the snapshot record exists in the state sync
    state_snapshot = ctx.state_sync.state_sync.get_snapshots([model1_snapshot.snapshot_id])
    assert state_snapshot

    # - Run the janitor again, this time it should succeed
    mocker.patch("sqlmesh.core.snapshot.evaluator.SnapshotEvaluator.cleanup")
    ctx._run_janitor(ignore_ttl=True)

    # - Check that the snapshot record does not exist in the state sync anymore
    state_snapshot = ctx.state_sync.state_sync.get_snapshots([model1_snapshot.snapshot_id])
    assert not state_snapshot

    # Case 2: Assume that the view cleanup yields an error, the enviroment
    # record should still exist
    mocker.patch(
        "sqlmesh.core.context.cleanup_expired_views",
        return_value=["view cleanup error"],
    )
    ctx, model1_snapshot = setup_scenario()

    views = ctx.fetchdf("FROM duckdb_views() SELECT * EXCLUDE(sql) WHERE NOT internal")
    assert views.empty

    # - Check that the environment record exists in the state sync
    assert ctx.state_sync.get_environment("dev")

    # - Run the janitor again, this time it should succeed
    mocker.patch("sqlmesh.core.context.cleanup_expired_views", return_value=[])
    ctx._run_janitor(ignore_ttl=True)

    # - Check that the environment record does not exist in the state sync anymore
    assert not ctx.state_sync.get_environment("dev")


def test_janitor_aggregates_failures_into_single_error(mocker: MockerFixture, tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "model1.sql").write_text("MODEL(name test.model1, kind FULL); SELECT 1 AS col")

    ctx = Context(
        paths=[tmp_path],
        config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb")),
    )
    ctx.plan("dev", no_prompts=True, auto_apply=True)
    ctx.invalidate_environment("dev")

    mocker.patch(
        "sqlmesh.core.context.cleanup_expired_views",
        return_value=["view drop error A", "view drop error B"],
    )
    mocker.patch(
        "sqlmesh.core.janitor.iter_expired_snapshot_batches",
        return_value=iter([]),
    )

    with pytest.raises(SQLMeshError, match="Janitor completed with failures"):
        ctx._run_janitor(ignore_ttl=True)


def test_janitor_warn_on_delete_failure_downgrades_aggregated_error(
    mocker: MockerFixture, tmp_path: Path
):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "model1.sql").write_text("MODEL(name test.model1, kind FULL); SELECT 1 AS col")

    ctx = Context(
        paths=[tmp_path],
        config=Config(
            model_defaults=ModelDefaultsConfig(dialect="duckdb"),
            janitor=JanitorConfig(warn_on_delete_failure=True),
        ),
    )
    ctx.plan("dev", no_prompts=True, auto_apply=True)
    ctx.invalidate_environment("dev")

    mocker.patch(
        "sqlmesh.core.context.cleanup_expired_views",
        return_value=["view drop error"],
    )
    mocker.patch(
        "sqlmesh.core.janitor.iter_expired_snapshot_batches",
        return_value=iter([]),
    )

    warn_spy = mocker.patch.object(ctx.console, "log_warning")

    ctx._run_janitor(ignore_ttl=True)
    assert warn_spy.called
    assert "Janitor completed with failures" in warn_spy.call_args[0][0]


def test_janitor_force_delete_removes_environment_state_despite_drop_failure(
    mocker: MockerFixture, tmp_path: Path
):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "model1.sql").write_text("MODEL(name test.model1, kind FULL); SELECT 1 AS col")

    ctx = Context(
        paths=[tmp_path],
        config=Config(model_defaults=ModelDefaultsConfig(dialect="duckdb")),
    )
    ctx.plan("dev", no_prompts=True, auto_apply=True)
    ctx.invalidate_environment("dev")

    mocker.patch(
        "sqlmesh.core.context.cleanup_expired_views",
        return_value=["view drop error"],
    )
    mocker.patch(
        "sqlmesh.core.janitor.iter_expired_snapshot_batches",
        return_value=iter([]),
    )

    # without force_delete the environment is retained for retry
    with pytest.raises(SQLMeshError):
        ctx._run_janitor(ignore_ttl=True, force_delete=False)
    assert ctx.state_sync.get_environment("dev") is not None

    # with force_delete the environment state is purged even though drops failed
    with pytest.raises(SQLMeshError):
        ctx._run_janitor(ignore_ttl=True, force_delete=True)
    assert ctx.state_sync.get_environment("dev") is None


def test_janitor_force_delete_removes_snapshot_state_despite_cleanup_failure(
    mocker: MockerFixture, tmp_path: Path
):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    model1_path = models_dir / "model1.sql"
    model1_path.write_text("MODEL(name test.model1, kind FULL); SELECT 1 AS col")

    # using warn_on_delete_failure so the janitor completes and we can inspect the state after
    ctx = Context(
        paths=[tmp_path],
        config=Config(
            model_defaults=ModelDefaultsConfig(dialect="duckdb"),
            janitor=JanitorConfig(warn_on_delete_failure=True),
        ),
    )
    ctx.plan("dev", no_prompts=True, auto_apply=True)
    model1_snapshot = ctx.get_snapshot("test.model1")

    # simulating a zombie snapshot
    model1_path.unlink()
    ctx.load()
    ctx.plan("dev", no_prompts=True, auto_apply=True)
    ctx.invalidate_environment("dev")

    mocker.patch(
        "sqlmesh.core.snapshot.evaluator.SnapshotEvaluator.cleanup",
        side_effect=Exception("table cleanup error"),
    )

    # without force_delete the snapshot state is retained for retry
    ctx._run_janitor(ignore_ttl=True, force_delete=False)
    assert ctx.state_sync.get_snapshots([model1_snapshot.snapshot_id])  # type: ignore

    # with force_delete the snapshot state record is purged even though cleanup failed
    ctx._run_janitor(ignore_ttl=True, force_delete=True)
    assert not ctx.state_sync.get_snapshots([model1_snapshot.snapshot_id])  # type: ignore


@use_terminal_console
def test_destroy(copy_to_temp_path):
    # Testing project with two gateways to verify cleanup is performed across engines
    paths = copy_to_temp_path("tests/fixtures/multi_virtual_layer")
    path = Path(paths[0])
    first_db_path = str(path / "db_1.db")
    second_db_path = str(path / "db_2.db")

    config = Config(
        gateways={
            "first": GatewayConfig(
                connection=DuckDBConnectionConfig(database=first_db_path),
                variables={"overriden_var": "gateway_1"},
            ),
            "second": GatewayConfig(
                connection=DuckDBConnectionConfig(database=second_db_path),
                variables={"overriden_var": "gateway_2"},
            ),
        },
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        model_naming=NameInferenceConfig(infer_names=True),
        default_gateway="first",
        gateway_managed_virtual_layer=True,
        variables={"overriden_var": "global", "global_one": 88},
    )

    context = Context(paths=paths, config=config)
    plan = context.plan_builder().build()
    assert len(plan.new_snapshots) == 4
    context.apply(plan)

    # Confirm cache exists
    cache_path = Path(path) / ".cache"
    assert cache_path.exists()
    assert len(list(cache_path.iterdir())) > 0

    model = context.get_model("db_1.first_schema.model_one")

    context.upsert_model(
        model.copy(
            update={
                "query_": ParsableSql(
                    sql=model.query.select("'c' AS extra").sql(dialect=model.dialect)
                )
            }
        )
    )
    plan = context.plan_builder().build()
    context.apply(plan)

    state_environments = context.state_reader.get_environments()
    state_snapshots = context.state_reader.get_snapshots(context.snapshots.values())

    assert len(state_snapshots) == len(state_environments[0].snapshots)

    # Create dev environment with changed models
    model = context.get_model("db_2.second_schema.model_one")
    context.upsert_model(
        model.copy(
            update={
                "query_": ParsableSql(
                    sql=model.query.select("'d' AS extra").sql(dialect=model.dialect)
                )
            }
        )
    )
    model = context.get_model("first_schema.model_two")
    context.upsert_model(
        model.copy(
            update={
                "query_": ParsableSql(
                    sql=model.query.select("'d2' AS col").sql(dialect=model.dialect)
                )
            }
        )
    )
    plan = context.plan_builder("dev").build()
    context.apply(plan)

    dev_environment = context.state_sync.get_environment("dev")
    assert dev_environment is not None

    state_environments = context.state_reader.get_environments()
    state_snapshots = context.state_reader.get_snapshots(context.snapshots.values())
    assert (
        len(state_snapshots)
        == len(state_environments[0].snapshots)
        == len(state_environments[1].snapshots)
    )

    # The state tables at this point should be able to be retrieved
    state_tables = {
        "_environments",
        "_snapshots",
        "_intervals",
        "_auto_restatements",
        "_environment_statements",
        "_intervals",
        "_versions",
    }
    for table_name in state_tables:
        context.fetchdf(f"SELECT * FROM db_1.sqlmesh.{table_name}")

    # The actual tables as well
    context.engine_adapters["second"].fetchdf(f"SELECT * FROM db_2.second_schema.model_one")
    context.engine_adapters["second"].fetchdf(f"SELECT * FROM db_2.second_schema.model_two")
    context.fetchdf(f"SELECT * FROM db_1.first_schema.model_one")
    context.fetchdf(f"SELECT * FROM db_1.first_schema.model_two")

    # Use the destroy command to remove all data objects and state
    # Mock the console confirmation to automatically return True
    with patch.object(context.console, "_confirm", return_value=True):
        context._destroy()

    # Ensure all tables have been removed
    for table_name in state_tables:
        with pytest.raises(
            Exception, match=f"Catalog Error: Table with name {table_name} does not exist!"
        ):
            context.fetchdf(f"SELECT * FROM db_1.sqlmesh.{table_name}")

    # Validate tables have been deleted as well
    with pytest.raises(
        Exception, match=r"Catalog Error: Table with name.*model_two.*does not exist"
    ):
        context.fetchdf("SELECT * FROM db_1.first_schema.model_two")
    with pytest.raises(
        Exception, match=r"Catalog Error: Table with name.*model_one.*does not exist"
    ):
        context.fetchdf("SELECT * FROM db_1.first_schema.model_one")

    with pytest.raises(
        Exception, match=r"Catalog Error: Table with name.*model_two.*does not exist"
    ):
        context.engine_adapters["second"].fetchdf("SELECT * FROM db_2.second_schema.model_two")
    with pytest.raises(
        Exception, match=r"Catalog Error: Table with name.*model_one.*does not exist"
    ):
        context.engine_adapters["second"].fetchdf("SELECT * FROM db_2.second_schema.model_one")

    # Ensure the cache has been removed
    assert not cache_path.exists()


@use_terminal_console
def test_render_path_instead_of_model(tmp_path: Path):
    create_temp_file(tmp_path, Path("models/test.sql"), "MODEL (name test_model); SELECT 1 AS col")
    ctx = Context(paths=tmp_path, config=Config())

    # Case 1: Fail gracefully when the user is passing in a path instead of a model name
    for test_model in ["models/test.sql", "models/test.py"]:
        with pytest.raises(
            SQLMeshError,
            match="Resolving models by path is not supported, please pass in the model name instead.",
        ):
            ctx.render(test_model)

    # Case 2: Fail gracefully when the model name is not found
    with pytest.raises(SQLMeshError, match="Cannot find model with name 'incorrect_model'"):
        ctx.render("incorrect_model")

    # Case 3: Render the model successfully
    assert ctx.render("test_model").sql() == 'SELECT 1 AS "col"'


def test_invalidating_environment(sushi_context: Context):
    apply_to_environment(sushi_context, "dev")
    start_environment = sushi_context.state_sync.get_environment("dev")
    assert start_environment is not None
    metadata = DuckDBMetadata.from_context(sushi_context)
    start_schemas = set(metadata.schemas)
    assert "sushi__dev" in start_schemas
    sushi_context.invalidate_environment("dev")
    invalidate_environment = sushi_context.state_sync.get_environment("dev")
    assert invalidate_environment is not None
    schemas_prior_to_janitor = set(metadata.schemas)
    assert invalidate_environment.expiration_ts < start_environment.expiration_ts  # type: ignore
    assert start_schemas == schemas_prior_to_janitor
    sushi_context._run_janitor()
    schemas_after_janitor = set(metadata.schemas)
    assert sushi_context.state_sync.get_environment("dev") is None
    assert start_schemas - schemas_after_janitor == {"sushi__dev"}


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_evaluate_uncategorized_snapshot(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Add a new projection
    model = context.get_model("sushi.waiter_revenue_by_day")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, model)))

    # Downstream model references the new projection
    downstream_model = context.get_model("sushi.top_waiters")
    context.upsert_model(add_projection_to_model(t.cast(SqlModel, downstream_model), literal=False))

    df = context.evaluate(
        "sushi.top_waiters", start="2023-01-05", end="2023-01-06", execution_time=now()
    )
    assert set(df["one"].tolist()) == {1}
