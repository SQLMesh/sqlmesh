# type: ignore
from __future__ import annotations

import pathlib
import typing as t
from datetime import datetime, timedelta
from IPython.utils.capture import capture_output

import time_machine
from pytest_mock.plugin import MockerFixture

import pytest
import time_machine

import sqlmesh
from sqlmesh import Config, Context
from sqlmesh.utils.date import now, to_datetime
from sqlmesh.utils.errors import SignalEvalError
from tests.core.engine_adapter.integration import (
    TestContext,
    TEST_SCHEMA,
)
from tests.utils.test_helpers import use_terminal_console

EVALUATION_SPY = None


@pytest.fixture(autouse=True)
def _skip_snowflake(ctx: TestContext):
    if ctx.dialect == "snowflake":
        # these tests use callbacks that need to run db queries within a time_travel context that changes the system time to be in the future
        # this causes invalid JWT's to be generated when the callbacks try to run a db query
        pytest.skip(
            "snowflake.connector generates an invalid JWT when time_travel changes the system time"
        )


# Mock the snapshot evaluator's evaluate function to count the number of times it is called
@pytest.fixture(autouse=True, scope="function")
def _install_evaluation_spy(mocker: MockerFixture):
    global EVALUATION_SPY
    EVALUATION_SPY = mocker.spy(sqlmesh.core.snapshot.evaluator.SnapshotEvaluator, "evaluate")
    yield
    EVALUATION_SPY = None


def assert_snapshot_last_altered_ts(
    context: Context,
    snapshot_id: str,
    last_altered_ts: datetime,
    dev_last_altered_ts: t.Optional[datetime] = None,
):
    """
    Ensure that prod and dev last altered timestamps of a snapshot are as expected.
    """
    snapshot = context.state_sync.get_snapshots([snapshot_id])[snapshot_id]

    if snapshot.is_external:
        return

    assert to_datetime(snapshot.last_altered_ts).replace(microsecond=0) == last_altered_ts.replace(
        microsecond=0
    )

    if dev_last_altered_ts:
        assert to_datetime(snapshot.dev_last_altered_ts).replace(
            microsecond=0
        ) == dev_last_altered_ts.replace(microsecond=0)


def assert_model_evaluation(
    lambda_func, was_evaluated: bool = True, day_delta: int = 0, model_evaluations: int = 1
):
    """
    Ensure that a model was evaluated by checking the freshness signal and that
    the evaluation function was called the expected number of times.
    """
    EVALUATION_SPY.reset_mock()
    timestamp = now(minute_floor=False) + timedelta(days=day_delta)
    with time_machine.travel(timestamp, tick=False):
        with capture_output() as output:
            plan_or_run_result = lambda_func()

    evaluate_function_called = EVALUATION_SPY.call_count == model_evaluations
    signal_was_checked = "Checking signals for" in output.stdout

    assert signal_was_checked
    if was_evaluated:
        assert "All ready" in output.stdout
        assert evaluate_function_called
    else:
        assert "None ready" in output.stdout
        assert not evaluate_function_called

    return timestamp, plan_or_run_result


def create_model(
    name: str, schema: str, query: str, path: pathlib.Path, signals: str = "freshness()"
):
    """
    Create a freshness model with the given name, path, and query.
    """
    model_name = f"{schema}.{name}"
    model_path = path / "models" / f"{name}.sql"
    (path / "models").mkdir(parents=True, exist_ok=True)
    model_path.write_text(
        f"""
        MODEL (
            name {model_name},
            start '2024-01-01',
            kind FULL,
            signals (
              {signals},
            )
        );

        {query}
    """
    )

    return model_name, model_path


def initialize_context(
    ctx: TestContext, tmp_path: pathlib.Path, num_external_models: int = 1
) -> t.Tuple[Context, str, t.List[str]]:
    """
    Initialize a context by creating a schema and external models.
    """
    adapter = ctx.engine_adapter
    if not adapter.SUPPORTS_METADATA_TABLE_LAST_MODIFIED_TS:
        pytest.skip("This test only runs for engines that support metadata-based freshness")

    # Create & initialize schema
    schema = ctx.add_test_suffix(TEST_SCHEMA)
    ctx._schemas.append(schema)
    adapter.create_schema(schema)

    # Create & initialize external models
    external_tables = []

    yaml_content = ""
    for i in range(1, num_external_models + 1):
        external_table = f"{schema}.external_table{i}"
        external_tables.append(f"{schema}.external_table{i}")
        adapter.execute(
            f"CREATE TABLE {external_table} AS (SELECT {i} AS col{i})",
            quote_identifiers=False,
        )

        yaml_content = (
            yaml_content
            + f"""
- name: {external_table}
  columns:
    col{i}: int

"""
        )

    external_models_yaml = tmp_path / "external_models.yaml"
    external_models_yaml.write_text(yaml_content)

    # Initialize context
    def _set_config(gateway: str, config: Config) -> None:
        config.model_defaults.dialect = ctx.dialect

    context = ctx.create_context(path=tmp_path, config_mutator=_set_config)

    return context, schema, external_tables


@use_terminal_console
def test_external_model_freshness(ctx: TestContext, tmp_path: pathlib.Path, mocker: MockerFixture):
    adapter = ctx.engine_adapter
    context, schema, (external_table1, external_table2) = initialize_context(
        ctx, tmp_path, num_external_models=2
    )

    # Create model that depends on external models
    model_name, model_path = create_model(
        "new_model",
        schema,
        f"SELECT col1 * col2 AS col FROM {external_table1}, {external_table2}",
        tmp_path,
    )

    context.load()

    # Case 1: Model is evaluated for the first plan
    prod_plan_ts_1, prod_plan_1 = assert_model_evaluation(
        lambda: context.plan(auto_apply=True, no_prompts=True)
    )

    prod_snapshot_id = next(iter(prod_plan_1.context_diff.new_snapshots))
    assert_snapshot_last_altered_ts(context, prod_snapshot_id, last_altered_ts=prod_plan_ts_1)

    # Case 2: Model is NOT evaluated on run if external models are not fresh
    assert_model_evaluation(lambda: context.run(), was_evaluated=False, day_delta=1)

    # Case 3: Differentiate last_altered_ts between snapshots with shared version
    # For instance, creating a FORWARD_ONLY change in dev (reusing the version but creating a dev preview) should not cause
    # any side effects to the prod snapshot's last_altered_ts hydration
    model_path.write_text(model_path.read_text().replace("col1 * col2", "col1 + col2"))
    context.load()
    dev_plan_ts = now(minute_floor=False) + timedelta(days=2)
    with time_machine.travel(dev_plan_ts, tick=False):
        dev_plan = context.plan(
            environment="dev", forward_only=True, auto_apply=True, no_prompts=True
        )

    context.state_sync.clear_cache()
    dev_snapshot_id = next(iter(dev_plan.context_diff.new_snapshots))
    assert_snapshot_last_altered_ts(
        context,
        dev_snapshot_id,
        last_altered_ts=prod_plan_ts_1,
        dev_last_altered_ts=dev_plan_ts,
    )
    assert_snapshot_last_altered_ts(context, prod_snapshot_id, last_altered_ts=prod_plan_ts_1)

    # Case 4: Model is evaluated on run if any external model is fresh
    adapter.execute(f"INSERT INTO {external_table2} (col2) VALUES (3)", quote_identifiers=False)
    assert_model_evaluation(lambda: context.run(), day_delta=2)

    # Case 5: Model is evaluated if changed (case 3) even if the external model is not fresh
    model_path.write_text(model_path.read_text().replace("col1 + col2", "col1 * col2 * 5"))
    context.load()
    assert_model_evaluation(
        lambda: context.plan(auto_apply=True, no_prompts=True),
        day_delta=3,
    )

    # Case 6: Model is evaluated on a restatement plan even if the external model is not fresh
    assert_model_evaluation(
        lambda: context.plan(restate_models=[model_name], auto_apply=True, no_prompts=True),
        day_delta=4,
    )


@use_terminal_console
def test_mixed_model_freshness(ctx: TestContext, tmp_path: pathlib.Path):
    """
    Scenario: Freshness for a model that depends on both external and SQLMesh models
    """

    adapter = ctx.engine_adapter
    context, schema, (external_table,) = initialize_context(ctx, tmp_path, num_external_models=1)

    # Create parent model that depends on the external model
    parent_model_name, _ = create_model(
        "parent_model",
        schema,
        f"SELECT col1 AS new_col FROM {external_table}",
        tmp_path,
    )

    # First child model depends only on the parent model
    create_model(
        "child_model1",
        schema,
        f"SELECT new_col FROM {parent_model_name}",
        tmp_path,
    )

    # Second child model depends on the parent model and the external table
    create_model(
        "child_model2",
        schema,
        f"SELECT col1 + new_col FROM {parent_model_name}, {external_table}",
        tmp_path,
    )

    # Third model does not depend on any models, so it should only be evaluated once
    create_model(
        "child_model3",
        schema,
        f"SELECT 1 AS col",
        tmp_path,
    )

    context.load()

    # Case 1: New models are evaluated when introduced in a plan
    prod_plan_ts_1, prod_plan_1 = assert_model_evaluation(
        lambda: context.plan(auto_apply=True, no_prompts=True),
        model_evaluations=4,
    )

    for new_snapshot in prod_plan_1.context_diff.new_snapshots:
        assert_snapshot_last_altered_ts(context, new_snapshot, last_altered_ts=prod_plan_ts_1)

    # Case 2: Mixed models are evaluated if the upstream models (sqlmesh or external) become fresh
    adapter.execute(f"INSERT INTO {external_table} (col1) VALUES (2)", quote_identifiers=False)

    assert_model_evaluation(
        lambda: context.run(), was_evaluated=True, day_delta=1, model_evaluations=3
    )

    # Case 3: Mixed models are still evaluated if breaking changes are introduced
    create_model(
        "child_model2",
        schema,
        f"SELECT col1 * new_col FROM {parent_model_name}, {external_table}",
        tmp_path,
    )

    context.load()

    prod_plan_ts_2, prod_plan_2 = assert_model_evaluation(
        lambda: context.plan(auto_apply=True, no_prompts=True),
        day_delta=1,
        model_evaluations=1,
    )

    assert prod_plan_2.context_diff.modified_snapshots

    assert_snapshot_last_altered_ts(
        context, next(iter(prod_plan_2.context_diff.new_snapshots)), last_altered_ts=prod_plan_ts_2
    )


def test_missing_external_model_freshness(ctx: TestContext, tmp_path: pathlib.Path):
    """
    Scenario: Freshness for a model that depends on an external model that is missing
    """
    adapter = ctx.engine_adapter
    context, schema, (external_table,) = initialize_context(ctx, tmp_path)

    # Create model that depends on the external model
    create_model(
        "new_model",
        schema,
        f"SELECT * FROM {external_table}",
        tmp_path,
    )

    context.load()
    context.plan(auto_apply=True, no_prompts=True)

    # Case: By dropping the external table, the freshness signal should raise an error
    # instead of silently succeeding/failing
    adapter.execute(f"DROP TABLE {external_table}", quote_identifiers=False)

    with time_machine.travel(now() + timedelta(days=1)):
        with pytest.raises(SignalEvalError):
            context.run()


@use_terminal_console
def test_check_ready_intervals(ctx: TestContext, tmp_path: pathlib.Path):
    """
    Scenario: Ensure that freshness evaluates the "ready" intervals of the parent snapshots i.e their
    missing intervals plus their signals applied.

    """

    def _write_user_signal(signal: str, tmp_path: pathlib.Path):
        signal_code = f"""
import typing as t
from sqlmesh import signal

@signal()
{signal}
      """

        test_signals = tmp_path / "signals/test_signals.py"
        test_signals.parent.mkdir(parents=True, exist_ok=True)
        test_signals.write_text(signal_code)

    context, schema, _ = initialize_context(ctx, tmp_path, num_external_models=0)

    _write_user_signal(
        """
def my_signal(batch): 
  return True
    """,
        tmp_path,
    )

    # Parent model depends on a custom signal
    parent_model, _ = create_model(
        "parent_model",
        schema,
        f"SELECT 1 AS col",
        tmp_path,
        signals="my_signal()",
    )

    # Create a new model that depends on the parent model
    create_model(
        "child_model",
        schema,
        f"SELECT * FROM {parent_model}",
        tmp_path,
    )

    # Case 1: Both models are evaluated when introduced in a plan and subsequent runs,
    # given that `my_signal()` always returns True.
    context.load()
    context.plan(auto_apply=True, no_prompts=True)

    assert_model_evaluation(
        lambda: context.run(),
        day_delta=2,
        model_evaluations=2,
    )

    # Case 2: By changing the signal to return False, both models should not be evaluated.
    _write_user_signal(
        """
def my_signal(batch): 
  return False
    """,
        tmp_path,
    )

    context.load()
    context.plan(auto_apply=True, no_prompts=True)

    assert_model_evaluation(
        lambda: context.run(),
        day_delta=3,
        was_evaluated=False,
    )


@use_terminal_console
def test_registered_and_unregistered_external_models(
    ctx: TestContext, tmp_path: pathlib.Path, mocker: MockerFixture
):
    """
    Scenario: Ensure that external models are queried for their last modified timestamp
    regardless of whether they are present in the "external_models.yaml" file (registered) or not (unregistered)
    """

    adapter = ctx.engine_adapter
    context, schema, (registered_external_table,) = initialize_context(
        ctx, tmp_path, num_external_models=1
    )

    current_catalog = ctx.engine_adapter.get_current_catalog()

    def normalize_external_table_name(external_table_name) -> str:
        from sqlglot import exp

        normalized = exp.normalize_table_name(
            f"{current_catalog}.{external_table_name}", dialect=ctx.dialect
        )
        return exp.table_name(normalized, dialect=ctx.dialect, identify=True)

    unregistered_external_table = f"{schema}.unregistered_external_table"

    adapter.execute(
        f"CREATE TABLE {unregistered_external_table} AS (SELECT 1 AS col)",
        quote_identifiers=False,
    )

    create_model(
        "new_model",
        schema,
        f"SELECT * FROM {unregistered_external_table}, {registered_external_table}",
        tmp_path,
    )

    context.load()
    context.plan(auto_apply=True, no_prompts=True)

    spy = mocker.spy(
        sqlmesh.core.engine_adapter.SnowflakeEngineAdapter, "get_table_last_modified_ts"
    )
    assert_model_evaluation(
        lambda: context.run(),
        day_delta=1,
        was_evaluated=False,
    )

    assert spy.call_args_list

    # The first argument of "get_table_last_modified_ts" is a list of external table names in normalized form
    # Ensure that this contains both external tables (registered and unregistered)
    assert sorted(spy.call_args[0][1]) == sorted(
        [
            normalize_external_table_name(registered_external_table),
            normalize_external_table_name(unregistered_external_table),
        ]
    )
