from __future__ import annotations

import typing as t
from collections import Counter
from datetime import timedelta
from unittest import mock
import pandas as pd  # noqa: TID253
import pytest
from pathlib import Path
import time_machine
from pytest_mock.plugin import MockerFixture
from sqlglot import exp

from sqlmesh import CustomMaterialization
from sqlmesh.core import dialect as d
from sqlmesh.core.config import (
    Config,
    ModelDefaultsConfig,
    DuckDBConnectionConfig,
    GatewayConfig,
)
from sqlmesh.core.console import Console
from sqlmesh.core.context import Context
from sqlmesh.core.config.categorizer import CategorizerConfig
from sqlmesh.core.model import (
    Model,
    SqlModel,
    CustomKind,
    load_sql_based_model,
)
from sqlmesh.core.plan import SnapshotIntervals
from sqlmesh.utils.date import to_date, to_timestamp
from sqlmesh.utils.pydantic import validate_string
from tests.conftest import SushiDataValidator
from sqlmesh.utils import CorrelationId
from tests.utils.test_filesystem import create_temp_file

if t.TYPE_CHECKING:
    from sqlmesh import QueryOrDF

pytestmark = pytest.mark.slow


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_incremental_by_partition(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    source_name = "raw.test_incremental_by_partition"
    model_name = "memory.sushi.test_incremental_by_partition"

    expressions = d.parse(
        f"""
        MODEL (
            name {model_name},
            kind INCREMENTAL_BY_PARTITION (disable_restatement false),
            partitioned_by [key],
            allow_partials true,
            start '2023-01-07',
        );

        SELECT key, value FROM {source_name};
        """
    )
    model = load_sql_based_model(expressions)
    context.upsert_model(model)

    context.engine_adapter.ctas(
        source_name,
        d.parse_one("SELECT 'key_a' AS key, 1 AS value"),
    )

    context.plan(auto_apply=True, no_prompts=True)
    assert context.engine_adapter.fetchall(f"SELECT * FROM {model_name}") == [
        ("key_a", 1),
    ]

    context.engine_adapter.replace_query(
        source_name,
        d.parse_one("SELECT 'key_b' AS key, 1 AS value"),
    )
    context.run(ignore_cron=True)
    assert context.engine_adapter.fetchall(f"SELECT * FROM {model_name}") == [
        ("key_a", 1),
        ("key_b", 1),
    ]

    context.engine_adapter.replace_query(
        source_name,
        d.parse_one("SELECT 'key_a' AS key, 2 AS value"),
    )
    # Run 1 minute later.
    with time_machine.travel("2023-01-08 15:01:00 UTC"):
        context.run(ignore_cron=True)
    assert context.engine_adapter.fetchall(f"SELECT * FROM {model_name}") == [
        ("key_b", 1),
        ("key_a", 2),
    ]

    # model should fully refresh on restatement
    context.engine_adapter.replace_query(
        source_name,
        d.parse_one("SELECT 'key_c' AS key, 3 AS value"),
    )
    context.plan(auto_apply=True, no_prompts=True, restate_models=[model_name])
    assert context.engine_adapter.fetchall(f"SELECT * FROM {model_name}") == [
        ("key_c", 3),
    ]


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_custom_materialization(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    custom_insert_called = False

    class CustomFullMaterialization(CustomMaterialization):
        NAME = "test_custom_full"

        def insert(
            self,
            table_name: str,
            query_or_df: QueryOrDF,
            model: Model,
            is_first_insert: bool,
            render_kwargs: t.Dict[str, t.Any],
            **kwargs: t.Any,
        ) -> None:
            nonlocal custom_insert_called
            custom_insert_called = True

            self._replace_query_for_model(model, table_name, query_or_df, render_kwargs)

    model = context.get_model("sushi.top_waiters")
    kwargs = {
        **model.dict(),
        # Make a breaking change.
        "kind": dict(name="CUSTOM", materialization="test_custom_full"),
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))

    context.plan(auto_apply=True, no_prompts=True)

    assert custom_insert_called


# needs to be defined at the top level. If its defined within the test body,
# adding to the snapshot cache fails with: AttributeError: Can't pickle local object
class TestCustomKind(CustomKind):
    __test__ = False  # prevent pytest warning since this isnt a class containing tests

    @property
    def custom_property(self) -> str:
        return validate_string(self.materialization_properties.get("custom_property"))


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_custom_materialization_with_custom_kind(init_and_plan_context: t.Callable):
    context, _ = init_and_plan_context("examples/sushi")

    custom_insert_calls = []

    class CustomFullMaterialization(CustomMaterialization[TestCustomKind]):
        NAME = "test_custom_full_with_custom_kind"

        def insert(
            self,
            table_name: str,
            query_or_df: QueryOrDF,
            model: Model,
            is_first_insert: bool,
            render_kwargs: t.Dict[str, t.Any],
            **kwargs: t.Any,
        ) -> None:
            assert isinstance(model.kind, TestCustomKind)

            nonlocal custom_insert_calls
            custom_insert_calls.append(model.kind.custom_property)

            self._replace_query_for_model(model, table_name, query_or_df, render_kwargs)

    model = context.get_model("sushi.top_waiters")
    kwargs = {
        **model.dict(),
        # Make a breaking change.
        "kind": dict(
            name="CUSTOM",
            materialization="test_custom_full_with_custom_kind",
            materialization_properties={"custom_property": "pytest"},
        ),
    }
    context.upsert_model(SqlModel.parse_obj(kwargs))

    context.plan(auto_apply=True)

    assert custom_insert_calls == ["pytest"]

    # no changes
    context.plan(auto_apply=True)

    assert custom_insert_calls == ["pytest"]

    # change a property on the custom kind, breaking change
    kwargs["kind"]["materialization_properties"]["custom_property"] = "some value"
    context.upsert_model(SqlModel.parse_obj(kwargs))
    context.plan(auto_apply=True)

    assert custom_insert_calls == ["pytest", "some value"]


def test_incremental_time_self_reference(
    mocker: MockerFixture, sushi_context: Context, sushi_data_validator: SushiDataValidator
):
    start_ts = to_timestamp("1 week ago")
    start_date, end_date = to_date("1 week ago"), to_date("yesterday")
    if to_timestamp(start_date) < start_ts:
        # The start date must be aligned by the interval unit.
        start_date += timedelta(days=1)

    df = sushi_context.engine_adapter.fetchdf(
        "SELECT MIN(event_date) FROM sushi.customer_revenue_lifetime"
    )
    assert df.iloc[0, 0] == pd.to_datetime(start_date)
    df = sushi_context.engine_adapter.fetchdf(
        "SELECT MAX(event_date) FROM sushi.customer_revenue_lifetime"
    )
    assert df.iloc[0, 0] == pd.to_datetime(end_date)
    results = sushi_data_validator.validate("sushi.customer_revenue_lifetime", start_date, end_date)
    plan = sushi_context.plan_builder(
        restate_models=["sushi.customer_revenue_lifetime", "sushi.customer_revenue_by_day"],
        start=start_date,
        end="5 days ago",
    ).build()
    revenue_lifeteime_snapshot = sushi_context.get_snapshot(
        "sushi.customer_revenue_lifetime", raise_if_missing=True
    )
    revenue_by_day_snapshot = sushi_context.get_snapshot(
        "sushi.customer_revenue_by_day", raise_if_missing=True
    )
    assert sorted(plan.missing_intervals, key=lambda x: x.snapshot_id) == sorted(
        [
            SnapshotIntervals(
                snapshot_id=revenue_lifeteime_snapshot.snapshot_id,
                intervals=[
                    (to_timestamp(to_date("7 days ago")), to_timestamp(to_date("6 days ago"))),
                    (to_timestamp(to_date("6 days ago")), to_timestamp(to_date("5 days ago"))),
                    (to_timestamp(to_date("5 days ago")), to_timestamp(to_date("4 days ago"))),
                    (to_timestamp(to_date("4 days ago")), to_timestamp(to_date("3 days ago"))),
                    (to_timestamp(to_date("3 days ago")), to_timestamp(to_date("2 days ago"))),
                    (to_timestamp(to_date("2 days ago")), to_timestamp(to_date("1 days ago"))),
                    (to_timestamp(to_date("1 day ago")), to_timestamp(to_date("today"))),
                ],
            ),
            SnapshotIntervals(
                snapshot_id=revenue_by_day_snapshot.snapshot_id,
                intervals=[
                    (to_timestamp(to_date("7 days ago")), to_timestamp(to_date("6 days ago"))),
                    (to_timestamp(to_date("6 days ago")), to_timestamp(to_date("5 days ago"))),
                ],
            ),
        ],
        key=lambda x: x.snapshot_id,
    )
    sushi_context.console = mocker.Mock(spec=Console)
    sushi_context.apply(plan)
    num_batch_calls = Counter(
        [x[0][0] for x in sushi_context.console.update_snapshot_evaluation_progress.call_args_list]  # type: ignore
    )
    # Validate that we made 7 calls to the customer_revenue_lifetime snapshot and 1 call to the customer_revenue_by_day snapshot
    assert num_batch_calls == {
        sushi_context.get_snapshot("sushi.customer_revenue_lifetime", raise_if_missing=True): 7,
        sushi_context.get_snapshot("sushi.customer_revenue_by_day", raise_if_missing=True): 1,
    }
    # Validate that the results are the same as before the restate
    assert results == sushi_data_validator.validate(
        "sushi.customer_revenue_lifetime", start_date, end_date
    )


def test_incremental_by_time_model_ignore_destructive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column ds,
            forward_only true,
            on_destructive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        *,
        1 as id,
        'test_name' as name,
        @start_ds as ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                2 as id,
                3 as new_column,
                @start_ds as ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns
        assert updated_df["new_column"].dropna().tolist() == [3]

    with time_machine.travel("2023-01-11 00:00:00 UTC"):
        updated_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                2 as id,
                CAST(4 AS STRING) as new_column,
                @start_ds as ds
            FROM
                source_table;
        """
        (models_dir / "test_model.sql").write_text(updated_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True, run=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 3
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns
        # The destructive change was ignored but this change is coercable and therefore we still return ints
        assert updated_df["new_column"].dropna().tolist() == [3, 4]

    with time_machine.travel("2023-01-12 00:00:00 UTC"):
        updated_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                2 as id,
                CAST(5 AS STRING) as new_column,
                @start_ds as ds
            FROM
                source_table;
        """
        (models_dir / "test_model.sql").write_text(updated_model)

        context = Context(paths=[tmp_path], config=config)
        # Make the change compatible since that means we will attempt and alter now that is considered additive
        context.engine_adapter.SCHEMA_DIFFER_KWARGS["compatible_types"] = {
            exp.DataType.build("INT"): {exp.DataType.build("STRING")}
        }
        context.plan("prod", auto_apply=True, no_prompts=True, run=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 4
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns
        # The change is now reflected since an additive alter could be performed
        assert updated_df["new_column"].dropna().tolist() == ["3", "4", "5"]

    context.close()


def test_incremental_by_time_model_ignore_additive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column ds,
            forward_only true,
            on_destructive_change allow,
            on_additive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        *,
        1 as id,
        'test_name' as name,
        'other' as other_column,
        @start_ds as ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column to the source table
        initial_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change allow,
                    on_additive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                1 as id,
                'other' as other_column,
                @start_ds as ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("ALTER TABLE source_table ADD COLUMN new_column INT")
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is removed since destructive is allowed
        assert "name" not in updated_df.columns
        # new_column is not added since additive is ignored
        assert "new_column" not in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was applied
        assert "name" not in updated_df.columns
        # new_column is still not added since additive is ignored
        assert "new_column" not in updated_df.columns

    with time_machine.travel("2023-01-11 00:00:00 UTC"):
        updated_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change allow,
                    on_additive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                CAST(1 AS STRING) as id,
                'other' as other_column,
                @start_ds as ds
            FROM
                source_table;
        """
        (models_dir / "test_model.sql").write_text(updated_model)

        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.SCHEMA_DIFFER_KWARGS["compatible_types"] = {
            exp.DataType.build("INT"): {exp.DataType.build("STRING")}
        }
        context.plan("prod", auto_apply=True, no_prompts=True, run=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 3
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is still not added since additive is ignored
        assert "new_column" not in updated_df.columns
        # The additive change was ignored since we set the change as compatible therefore
        # instead of getting strings in the result we still return ints
        assert updated_df["id"].tolist() == [1, 1, 1]

    with time_machine.travel("2023-01-12 00:00:00 UTC"):
        updated_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change allow,
                    on_additive_change allow
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                CAST(1 AS STRING) as id,
                'other' as other_column,
                @start_ds as ds
            FROM
                source_table;
        """
        (models_dir / "test_model.sql").write_text(updated_model)

        context = Context(paths=[tmp_path], config=config)
        # Make the change compatible since that means we will attempt and alter now that is considered additive
        context.engine_adapter.SCHEMA_DIFFER_KWARGS["compatible_types"] = {
            exp.DataType.build("INT"): {exp.DataType.build("STRING")}
        }
        context.plan("prod", auto_apply=True, no_prompts=True, run=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 4
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is now added since it is additive is now allowed
        assert "new_column" in updated_df.columns
        # The change is now reflected since an additive alter could be performed
        assert updated_df["id"].dropna().tolist() == ["1", "1", "1", "1"]

    context.close()


def test_incremental_by_unique_key_model_ignore_destructive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind INCREMENTAL_BY_UNIQUE_KEY (
            unique_key id,
            forward_only true,
            on_destructive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        *,
        1 as id,
        'test_name' as name,
        @start_ds as ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_UNIQUE_KEY (
                    unique_key id,
                    forward_only true,
                    on_destructive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                2 as id,
                3 as new_column,
                @start_ds as ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

    context.close()


def test_incremental_by_unique_key_model_ignore_additive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind INCREMENTAL_BY_UNIQUE_KEY (
            unique_key id,
            forward_only true,
            on_destructive_change allow,
            on_additive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        *,
        1 as id,
        'test_name' as name,
        @start_ds as ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_UNIQUE_KEY (
                    unique_key id,
                    forward_only true,
                    on_destructive_change allow,
                    on_additive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                2 as id,
                3 as new_column,
                @start_ds as ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still not in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

    context.close()


def test_incremental_unmanaged_model_ignore_destructive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind INCREMENTAL_UNMANAGED(
            on_destructive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        *,
        1 as id,
        'test_name' as name,
        @start_ds as ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_UNMANAGED(
                    on_destructive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                2 as id,
                3 as new_column,
                @start_ds as ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

    context.close()


def test_incremental_unmanaged_model_ignore_additive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind INCREMENTAL_UNMANAGED(
            on_destructive_change allow,
            on_additive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        *,
        1 as id,
        'test_name' as name,
        @start_ds as ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_UNMANAGED(
                    on_destructive_change allow,
                    on_additive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                2 as id,
                3 as new_column,
                @start_ds as ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

    context.close()


def test_scd_type_2_by_time_ignore_destructive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind SCD_TYPE_2_BY_TIME (
            unique_key id,
            updated_at_name ds,
            on_destructive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        *,
        1 as id,
        'test_name' as name,
        @start_dt as ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind SCD_TYPE_2_BY_TIME (
                    unique_key id,
                    updated_at_name ds,
                    on_destructive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                1 as id,
                3 as new_column,
                @start_dt as ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

    context.close()


def test_scd_type_2_by_time_ignore_additive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind SCD_TYPE_2_BY_TIME (
            unique_key id,
            updated_at_name ds,
            on_destructive_change allow,
            on_additive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        *,
        1 as id,
        'test_name' as name,
        @start_dt as ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind SCD_TYPE_2_BY_TIME (
                    unique_key id,
                    updated_at_name ds,
                    on_destructive_change allow,
                    on_additive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                *,
                1 as id,
                3 as new_column,
                @start_dt as ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

    context.close()


def test_scd_type_2_by_column_ignore_destructive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
        MODEL (
            name test_model,
            kind SCD_TYPE_2_BY_COLUMN (
                unique_key id,
                columns [name],
                on_destructive_change ignore
            ),
            start '2023-01-01',
            cron '@daily'
        );

        SELECT
            *,
            1 as id,
            'test_name' as name,
            @start_ds as ds
        FROM
            source_table;
        """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
        MODEL (
            name test_model,
            kind SCD_TYPE_2_BY_COLUMN (
                unique_key id,
                columns [new_column],
                on_destructive_change ignore
            ),
            start '2023-01-01',
            cron '@daily'
        );

        SELECT
            *,
            1 as id,
            3 as new_column,
            @start_ds as ds
        FROM
            source_table;
        """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

    context.close()


def test_scd_type_2_by_column_ignore_additive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
        MODEL (
            name test_model,
            kind SCD_TYPE_2_BY_COLUMN (
                unique_key id,
                columns [stable],
                on_destructive_change allow,
                on_additive_change ignore
            ),
            start '2023-01-01',
            cron '@daily'
        );

        SELECT
            *,
            1 as id,
            'test_name' as name,
            'stable' as stable,
            @start_ds as ds
        FROM
            source_table;
        """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
        MODEL (
            name test_model,
            kind SCD_TYPE_2_BY_COLUMN (
                unique_key id,
                columns [stable],
                on_destructive_change allow,
                on_additive_change ignore
            ),
            start '2023-01-01',
            cron '@daily'
        );

        SELECT
            *,
            1 as id,
            'stable2' as stable,
            3 as new_column,
            @start_ds as ds
        FROM
            source_table;
        """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was ignored
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

    context.close()


def test_incremental_partition_ignore_destructive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
        MODEL (
            name test_model,
            kind INCREMENTAL_BY_PARTITION (
                on_destructive_change ignore
            ),
            partitioned_by [ds],
            start '2023-01-01',
            cron '@daily'
        );

        SELECT
            *,
            1 as id,
            'test_name' as name,
            @start_ds as ds
        FROM
            source_table;
        """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
        MODEL (
            name test_model,
            kind INCREMENTAL_BY_PARTITION (
                on_destructive_change ignore
            ),
            partitioned_by [ds],
            start '2023-01-01',
            cron '@daily'
        );

        SELECT
            *,
            1 as id,
            3 as new_column,
            @start_ds as ds
        FROM
            source_table;
        """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns

    context.close()


def test_incremental_partition_ignore_additive_change(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
        MODEL (
            name test_model,
            kind INCREMENTAL_BY_PARTITION (
                on_destructive_change allow,
                on_additive_change ignore
            ),
            partitioned_by [ds],
            start '2023-01-01',
            cron '@daily'
        );

        SELECT
            *,
            1 as id,
            'test_name' as name,
            @start_ds as ds
        FROM
            source_table;
        """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("CREATE TABLE source_table (source_id INT)")
        context.engine_adapter.execute("INSERT INTO source_table VALUES (1)")

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns

        context.close()

        # remove `name` column and add new column
        initial_model = """
        MODEL (
            name test_model,
            kind INCREMENTAL_BY_PARTITION (
                on_destructive_change allow,
                on_additive_change ignore
            ),
            partitioned_by [ds],
            start '2023-01-01',
            cron '@daily'
        );

        SELECT
            *,
            1 as id,
            3 as new_column,
            @start_ds as ds
        FROM
            source_table;
        """
        (models_dir / "test_model.sql").write_text(initial_model)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True)

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')

        assert len(updated_df) == 1
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.run()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "source_id" in initial_df.columns
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not still in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns

    context.close()


def test_incremental_by_time_model_ignore_destructive_change_unit_test(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    test_filepath = test_dir / "test_test_model.yaml"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column ds,
            forward_only true,
            on_destructive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        id,
        name,
        ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    initial_test = f"""

test_test_model:
  model: test_model
  inputs:
    source_table:
      - id: 1
        name: 'test_name'
        ds: '2025-01-01'
  outputs:
    query:
      - id: 1
        name: 'test_name'
        ds: '2025-01-01'
"""

    # Write initial test
    test_filepath.write_text(initial_test)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute(
            "CREATE TABLE source_table (id INT, name STRING, new_column INT, ds STRING)"
        )
        context.engine_adapter.execute(
            "INSERT INTO source_table VALUES (1, 'test_name', NULL, '2023-01-01')"
        )

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)
        test_result = context.test()

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns
        assert len(test_result.successes) == 1
        assert test_result.testsRun == len(test_result.successes)

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                id,
                new_column,
                ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        updated_test = f"""

        test_test_model:
          model: test_model
          inputs:
            source_table:
              - id: 1
                new_column: 3
                ds: '2025-01-01'
          outputs:
            query:
              - id: 1
                new_column: 3
                ds: '2025-01-01'
        """

        # Write initial test
        test_filepath.write_text(updated_test)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)
        test_result = context.test()

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 1
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns
        assert len(test_result.successes) == 1
        assert test_result.testsRun == len(test_result.successes)

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("INSERT INTO source_table VALUES (2, NULL, 3, '2023-01-09')")
        context.run()
        test_result = context.test()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still in table since destructive was ignored
        assert "name" in updated_df.columns
        # new_column is added since it is additive and allowed
        assert "new_column" in updated_df.columns
        assert len(test_result.successes) == 1
        assert test_result.testsRun == len(test_result.successes)

    context.close()


def test_incremental_by_time_model_ignore_additive_change_unit_test(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_filepath = data_dir / "test.duckdb"
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    test_filepath = test_dir / "test_test_model.yaml"

    config = Config(
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        default_connection=DuckDBConnectionConfig(database=str(data_filepath)),
    )

    # Initial model with 3 columns
    initial_model = f"""
    MODEL (
        name test_model,
        kind INCREMENTAL_BY_TIME_RANGE (
            time_column ds,
            forward_only true,
            on_destructive_change allow,
            on_additive_change ignore
        ),
        start '2023-01-01',
        cron '@daily'
    );

    SELECT
        id,
        name,
        ds
    FROM
        source_table;
    """

    # Write initial model
    (models_dir / "test_model.sql").write_text(initial_model)

    initial_test = f"""

test_test_model:
  model: test_model
  inputs:
    source_table:
      - id: 1
        name: 'test_name'
        ds: '2025-01-01'
  outputs:
    query:
      - id: 1
        name: 'test_name'
        ds: '2025-01-01'
"""

    # Write initial test
    test_filepath.write_text(initial_test)

    with time_machine.travel("2023-01-08 00:00:00 UTC"):
        # Create context and apply initial model
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute(
            "CREATE TABLE source_table (id INT, name STRING, new_column INT, ds STRING)"
        )
        context.engine_adapter.execute(
            "INSERT INTO source_table VALUES (1, 'test_name', NULL, '2023-01-01')"
        )

        # Apply initial plan and load data
        context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)
        test_result = context.test()

        # Verify initial data was loaded
        initial_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(initial_df) == 1
        assert "id" in initial_df.columns
        assert "name" in initial_df.columns
        assert "ds" in initial_df.columns
        assert len(test_result.successes) == 1
        assert test_result.testsRun == len(test_result.successes)

        context.close()

        # remove `name` column and add new column
        initial_model = """
            MODEL (
                name test_model,
                kind INCREMENTAL_BY_TIME_RANGE (
                    time_column ds,
                    forward_only true,
                    on_destructive_change allow,
                    on_additive_change ignore
                ),
                start '2023-01-01',
                cron '@daily'
            );

            SELECT
                id,
                new_column,
                ds
            FROM
                source_table;
            """
        (models_dir / "test_model.sql").write_text(initial_model)

        # `new_column` is in the output since unit tests are based on the model definition that currently
        # exists and doesn't take into account the historical changes to the table. Therefore `new_column` is
        # not actually in the table but it is represented in the test
        updated_test = f"""
        test_test_model:
          model: test_model
          inputs:
            source_table:
              - id: 1
                new_column: 3
                ds: '2025-01-01'
          outputs:
            query:
              - id: 1
                new_column: 3
                ds: '2025-01-01'
        """

        # Write initial test
        test_filepath.write_text(updated_test)

        context = Context(paths=[tmp_path], config=config)
        context.plan("prod", auto_apply=True, no_prompts=True, skip_tests=True)
        test_result = context.test()

        # Verify data loading continued to work
        # The existing data should still be there and new data should be loaded
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 1
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is not in table since destructive was ignored
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns
        assert len(test_result.successes) == 1
        assert test_result.testsRun == len(test_result.successes)

        context.close()

    with time_machine.travel("2023-01-10 00:00:00 UTC"):
        context = Context(paths=[tmp_path], config=config)
        context.engine_adapter.execute("INSERT INTO source_table VALUES (2, NULL, 3, '2023-01-09')")
        context.run()
        test_result = context.test()
        updated_df = context.fetchdf('SELECT * FROM "default"."test_model"')
        assert len(updated_df) == 2
        assert "id" in updated_df.columns
        assert "ds" in updated_df.columns
        # name is still not in table since destructive was allowed
        assert "name" not in updated_df.columns
        # new_column is not added since it is additive and ignored
        assert "new_column" not in updated_df.columns
        assert len(test_result.successes) == 1
        assert test_result.testsRun == len(test_result.successes)

    context.close()


@time_machine.travel("2020-01-01 00:00:00 UTC")
def test_scd_type_2_full_restatement_no_start_date(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    # Initial product catalog of 3 products
    raw_products = d.parse("""
    MODEL (
        name memory.store.raw_products,
        kind FULL
    );

    SELECT * FROM VALUES
        (101, 'Laptop Pro', 1299.99, 'Electronics', '2020-01-01 00:00:00'::TIMESTAMP),
        (102, 'Wireless Mouse', 49.99, 'Electronics', '2020-01-01 00:00:00'::TIMESTAMP),
        (103, 'Office Chair', 199.99, 'Furniture', '2020-01-01 00:00:00'::TIMESTAMP)
    AS t(product_id, product_name, price, category, last_updated);
    """)

    # SCD Type 2 model for product history tracking
    product_history = d.parse("""
    MODEL (
        name memory.store.product_history,
        kind SCD_TYPE_2_BY_TIME (
            unique_key product_id,
            updated_at_name last_updated,
            disable_restatement false
        ),
        owner catalog_team,
        cron '0 */6 * * *',
        grain product_id,
        description 'Product catalog change history'
    );

    SELECT
        product_id::INT AS product_id,
        product_name::TEXT AS product_name,
        price::DECIMAL(10,2) AS price,
        category::TEXT AS category,
        last_updated AS last_updated
    FROM
        memory.store.raw_products;
    """)

    raw_products_model = load_sql_based_model(raw_products)
    product_history_model = load_sql_based_model(product_history)
    context.upsert_model(raw_products_model)
    context.upsert_model(product_history_model)

    # Initial plan and apply
    plan = context.plan_builder("prod", skip_tests=True).build()
    context.apply(plan)

    query = "SELECT product_id, product_name, price, category, last_updated, valid_from, valid_to FROM memory.store.product_history ORDER BY product_id, valid_from"
    initial_data = context.engine_adapter.fetchdf(query)

    # Validate initial state of 3 products all active
    assert len(initial_data) == 3
    assert initial_data["valid_to"].isna().all()
    initial_product_names = set(initial_data["product_name"].tolist())
    assert initial_product_names == {"Laptop Pro", "Wireless Mouse", "Office Chair"}

    # Price update and category change
    with time_machine.travel("2020-01-15 12:00:00 UTC"):
        raw_products_v2 = d.parse("""
        MODEL (
            name memory.store.raw_products,
            kind FULL
        );

        SELECT * FROM VALUES
            (101, 'Laptop Pro', 1199.99, 'Electronics', '2020-01-15 00:00:00'::TIMESTAMP),
            (102, 'Wireless Mouse', 49.99, 'Electronics', '2020-01-01 00:00:00'::TIMESTAMP),
            (103, 'Ergonomic Office Chair', 229.99, 'Office Furniture', '2020-01-15 00:00:00'::TIMESTAMP)
        AS t(product_id, product_name, price, category, last_updated);
        """)
        raw_products_v2_model = load_sql_based_model(raw_products_v2)
        context.upsert_model(raw_products_v2_model)
        context.plan(
            auto_apply=True, no_prompts=True, categorizer_config=CategorizerConfig.all_full()
        )
        context.run()

        data_after_first_change = context.engine_adapter.fetchdf(query)

        # Should have 5 records (3 original closed,  2 new activε, 1 unchanged)
        assert len(data_after_first_change) == 5

    # Second change
    with time_machine.travel("2020-02-01 10:00:00 UTC"):
        raw_products_v3 = d.parse("""
        MODEL (
            name memory.store.raw_products,
            kind FULL
        );

        SELECT * FROM VALUES
            (101, 'Laptop Pro Max', 1399.99, 'Electronics', '2020-02-01 00:00:00'::TIMESTAMP),
            (103, 'Ergonomic Office Chair', 229.99, 'Office Furniture', '2020-01-15 00:00:00'::TIMESTAMP),
            (102, 'Wireless Mouse', 49.99, 'Electronics', '2020-01-01 00:00:00'::TIMESTAMP)
        AS t(product_id, product_name, price, category, last_updated);
        """)
        raw_products_v3_model = load_sql_based_model(raw_products_v3)
        context.upsert_model(raw_products_v3_model)
        context.plan(
            auto_apply=True, no_prompts=True, categorizer_config=CategorizerConfig.all_full()
        )
        context.run()
        data_after_second_change = context.engine_adapter.fetchdf(query)
        assert len(data_after_second_change) == 6

    # Store the current state before full restatement
    data_before_full_restatement = data_after_second_change.copy()

    # Perform full restatement (no start date provided)
    with time_machine.travel("2020-02-01 15:00:00 UTC"):
        plan = context.plan_builder(
            "prod", skip_tests=True, restate_models=["memory.store.product_history"]
        ).build()
        context.apply(plan)
        data_after_full_restatement = context.engine_adapter.fetchdf(query)
        assert len(data_after_full_restatement) == 3

        # Check that all currently active products before restatement are still active after restatement
        active_before = data_before_full_restatement[
            data_before_full_restatement["valid_to"].isna()
        ]
        active_after = data_after_full_restatement
        assert set(active_before["product_id"]) == set(active_after["product_id"])

        expected_products = {
            101: {
                "product_name": "Laptop Pro Max",
                "price": 1399.99,
                "category": "Electronics",
                "last_updated": "2020-02-01",
            },
            102: {
                "product_name": "Wireless Mouse",
                "price": 49.99,
                "category": "Electronics",
                "last_updated": "2020-01-01",
            },
            103: {
                "product_name": "Ergonomic Office Chair",
                "price": 229.99,
                "category": "Office Furniture",
                "last_updated": "2020-01-15",
            },
        }
        for _, row in data_after_full_restatement.iterrows():
            pid = row["product_id"]
            assert pid in expected_products
            expected = expected_products[pid]
            assert row["product_name"] == expected["product_name"]
            assert float(row["price"]) == expected["price"]
            assert row["category"] == expected["category"]

            # valid_from should be the epoch, valid_to should be NaT
            assert str(row["valid_from"]) == "1970-01-01 00:00:00"
            assert pd.isna(row["valid_to"])


def test_plan_evaluator_correlation_id(tmp_path: Path):
    def _correlation_id_in_sqls(correlation_id: CorrelationId, mock_logger):
        sqls = [call[0][0] for call in mock_logger.call_args_list]
        return any(f"/* {correlation_id} */" in sql for sql in sqls)

    ctx = Context(paths=[tmp_path], config=Config())

    # Case: Ensure that the correlation id (plan_id) is included in the SQL for each plan
    for i in range(2):
        create_temp_file(
            tmp_path,
            Path("models", "test.sql"),
            f"MODEL (name test.a, kind FULL); SELECT {i} AS col",
        )

        with mock.patch("sqlmesh.core.engine_adapter.base.EngineAdapter._log_sql") as mock_logger:
            ctx.load()
            plan = ctx.plan(auto_apply=True, no_prompts=True)

        correlation_id = CorrelationId.from_plan_id(plan.plan_id)
        assert str(correlation_id) == f"SQLMESH_PLAN: {plan.plan_id}"

        assert _correlation_id_in_sqls(correlation_id, mock_logger)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_run_correlation_id(tmp_path: Path):
    def _correlation_id_in_sqls(correlation_id: CorrelationId, mock_logger):
        sqls = [call[0][0] for call in mock_logger.call_args_list]
        return any(f"/* {correlation_id} */" in sql for sql in sqls)

    run_id = "test_run_id"
    ctx = Context(paths=[tmp_path], config=Config())

    create_temp_file(
        tmp_path,
        Path("models", "test.sql"),
        """
MODEL (
    name test.a,
    kind INCREMENTAL_BY_TIME_RANGE (
        time_column event_ts
    ),
    cron '@daily',
    start '2023-01-07'
);
SELECT 1 AS col, '2023-01-07' AS event_ts
""",
    )

    ctx.load()
    ctx.plan(auto_apply=True, no_prompts=True)

    with time_machine.travel("2023-01-09 00:00:00 UTC"):
        with mock.patch(
            "sqlmesh.core.context.analytics.collector.on_run_start", return_value=run_id
        ):
            with mock.patch(
                "sqlmesh.core.engine_adapter.base.EngineAdapter._log_sql"
            ) as mock_logger:
                ctx.run()

    correlation_id = CorrelationId.from_run_id(run_id)
    assert str(correlation_id) == f"SQLMESH_RUN: {run_id}"
    assert _correlation_id_in_sqls(correlation_id, mock_logger)


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_scd_type_2_regular_run_with_offset(init_and_plan_context: t.Callable):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    raw_employee_status = d.parse("""
    MODEL (
        name memory.hr_system.raw_employee_status,
        kind FULL
    );

    SELECT
        1001 AS employee_id,
        'engineering' AS department,
        'EMEA' AS region,
        '2023-01-08 15:00:00 UTC' AS last_modified;
    """)

    employee_history = d.parse("""
    MODEL (
        name memory.hr_system.employee_history,
        kind SCD_TYPE_2_BY_TIME (
            unique_key employee_id,
            updated_at_name last_modified,
            disable_restatement false
        ),
        owner hr_analytics,
        cron '0 7 * * *',
        grain employee_id,
        description 'Historical tracking of employee status changes'
    );

    SELECT
        employee_id::INT AS employee_id,
        department::TEXT AS department,
        region::TEXT AS region,
        last_modified AS last_modified
    FROM
        memory.hr_system.raw_employee_status;
    """)

    raw_employee_status_model = load_sql_based_model(raw_employee_status)
    employee_history_model = load_sql_based_model(employee_history)
    context.upsert_model(raw_employee_status_model)
    context.upsert_model(employee_history_model)

    # Initial plan and apply
    plan = context.plan_builder("prod", skip_tests=True).build()
    context.apply(plan)

    query = "SELECT employee_id, department, region, valid_from, valid_to FROM memory.hr_system.employee_history ORDER BY employee_id, valid_from"
    initial_data = context.engine_adapter.fetchdf(query)

    assert len(initial_data) == 1
    assert initial_data["valid_to"].isna().all()
    assert initial_data["department"].tolist() == ["engineering"]
    assert initial_data["region"].tolist() == ["EMEA"]

    # Apply a future plan with source changes a few hours before the cron time of the SCD Type 2 model BUT on the same day
    with time_machine.travel("2023-01-09 00:10:00 UTC"):
        raw_employee_status_v2 = d.parse("""
        MODEL (
            name memory.hr_system.raw_employee_status,
            kind FULL
        );

        SELECT
            1001 AS employee_id,
            'engineering' AS department,
            'AMER' AS region,
            '2023-01-09 00:10:00 UTC' AS last_modified;
        """)
        raw_employee_status_v2_model = load_sql_based_model(raw_employee_status_v2)
        context.upsert_model(raw_employee_status_v2_model)
        context.plan(
            auto_apply=True, no_prompts=True, categorizer_config=CategorizerConfig.all_full()
        )

    # The 7th hour of the day the run is kicked off for the SCD Type 2 model
    with time_machine.travel("2023-01-09 07:00:01 UTC"):
        context.run()
        data_after_change = context.engine_adapter.fetchdf(query)

        # Validate the SCD2 records for employee 1001
        assert len(data_after_change) == 2
        assert data_after_change.iloc[0]["employee_id"] == 1001
        assert data_after_change.iloc[0]["department"] == "engineering"
        assert data_after_change.iloc[0]["region"] == "EMEA"
        assert str(data_after_change.iloc[0]["valid_from"]) == "1970-01-01 00:00:00"
        assert str(data_after_change.iloc[0]["valid_to"]) == "2023-01-09 00:10:00"
        assert data_after_change.iloc[1]["employee_id"] == 1001
        assert data_after_change.iloc[1]["department"] == "engineering"
        assert data_after_change.iloc[1]["region"] == "AMER"
        assert str(data_after_change.iloc[1]["valid_from"]) == "2023-01-09 00:10:00"
        assert pd.isna(data_after_change.iloc[1]["valid_to"])

        # Update source model again a bit later on the same day
        raw_employee_status_v2 = d.parse("""
        MODEL (
            name memory.hr_system.raw_employee_status,
            kind FULL
        );

        SELECT
            1001 AS employee_id,
            'sales' AS department,
            'ANZ' AS region,
            '2023-01-09 07:26:00 UTC' AS last_modified;
        """)
        raw_employee_status_v2_model = load_sql_based_model(raw_employee_status_v2)
        context.upsert_model(raw_employee_status_v2_model)
        context.plan(
            auto_apply=True, no_prompts=True, categorizer_config=CategorizerConfig.all_full()
        )

    # A day later the run is kicked off for the SCD Type 2 model again
    with time_machine.travel("2023-01-10 07:00:00 UTC"):
        context.run()
        data_after_change = context.engine_adapter.fetchdf(query)

        # Validate the SCD2 history for employee 1001 after second change with the historical records intact
        assert len(data_after_change) == 3
        assert data_after_change.iloc[0]["employee_id"] == 1001
        assert data_after_change.iloc[0]["department"] == "engineering"
        assert data_after_change.iloc[0]["region"] == "EMEA"
        assert str(data_after_change.iloc[0]["valid_from"]) == "1970-01-01 00:00:00"
        assert str(data_after_change.iloc[0]["valid_to"]) == "2023-01-09 00:10:00"
        assert data_after_change.iloc[1]["employee_id"] == 1001
        assert data_after_change.iloc[1]["department"] == "engineering"
        assert data_after_change.iloc[1]["region"] == "AMER"
        assert str(data_after_change.iloc[1]["valid_from"]) == "2023-01-09 00:10:00"
        assert str(data_after_change.iloc[1]["valid_to"]) == "2023-01-09 07:26:00"
        assert data_after_change.iloc[2]["employee_id"] == 1001
        assert data_after_change.iloc[2]["department"] == "sales"
        assert data_after_change.iloc[2]["region"] == "ANZ"
        assert str(data_after_change.iloc[2]["valid_from"]) == "2023-01-09 07:26:00"
        assert pd.isna(data_after_change.iloc[2]["valid_to"])

    # Now test restatement works (full restatement support currently)
    with time_machine.travel("2023-01-10 07:38:00 UTC"):
        plan = context.plan_builder(
            "prod",
            skip_tests=True,
            restate_models=["memory.hr_system.employee_history"],
            start="2023-01-09 00:10:00",
        ).build()
        context.apply(plan)
        restated_data = context.engine_adapter.fetchdf(query)

        # Validate the SCD2 history after restatement has been wiped bar one
        assert len(restated_data) == 1
        assert restated_data.iloc[0]["employee_id"] == 1001
        assert restated_data.iloc[0]["department"] == "sales"
        assert restated_data.iloc[0]["region"] == "ANZ"
        assert str(restated_data.iloc[0]["valid_from"]) == "1970-01-01 00:00:00"
        assert pd.isna(restated_data.iloc[0]["valid_to"])


def test_seed_model_metadata_update_does_not_trigger_backfill(tmp_path: Path):
    """
    Scenario:
        - Create a seed model; perform initial population
        - Modify the model with a metadata-only change and trigger a plan

    Outcome:
        - The seed model is modified (metadata-only) but this should NOT trigger backfill
        - There should be no missing_intervals on the plan to backfill
    """

    models_path = tmp_path / "models"
    seeds_path = tmp_path / "seeds"
    models_path.mkdir()
    seeds_path.mkdir()

    seed_model_path = models_path / "seed.sql"
    seed_path = seeds_path / "seed_data.csv"

    seed_path.write_text("\n".join(["id,name", "1,test"]))

    seed_model_path.write_text("""
    MODEL (
        name test.source_data,
        kind SEED (
            path '../seeds/seed_data.csv'
        )
    );
    """)

    config = Config(
        gateways={"": GatewayConfig(connection=DuckDBConnectionConfig())},
        model_defaults=ModelDefaultsConfig(dialect="duckdb", start="2024-01-01"),
    )
    ctx = Context(paths=tmp_path, config=config)

    plan = ctx.plan(auto_apply=True)

    original_seed_snapshot = ctx.snapshots['"memory"."test"."source_data"']
    assert plan.directly_modified == {original_seed_snapshot.snapshot_id}
    assert plan.metadata_updated == set()
    assert plan.missing_intervals

    # prove data loaded
    assert ctx.engine_adapter.fetchall("select id, name from memory.test.source_data") == [
        (1, "test")
    ]

    # prove no diff
    ctx.load()
    plan = ctx.plan(auto_apply=True)
    assert not plan.has_changes
    assert not plan.missing_intervals

    # make metadata-only change
    seed_model_path.write_text("""
    MODEL (
        name test.source_data,
        kind SEED (
            path '../seeds/seed_data.csv'
        ),
        description 'updated by test'
    );
    """)

    ctx.load()
    plan = ctx.plan(auto_apply=True)
    assert plan.has_changes

    new_seed_snapshot = ctx.snapshots['"memory"."test"."source_data"']
    assert (
        new_seed_snapshot.version == original_seed_snapshot.version
    )  # should be using the same physical table
    assert (
        new_seed_snapshot.snapshot_id != original_seed_snapshot.snapshot_id
    )  # but still be different due to the metadata change
    assert plan.directly_modified == set()
    assert plan.metadata_updated == {new_seed_snapshot.snapshot_id}

    # there should be no missing intervals to backfill since all we did is update a description
    assert not plan.missing_intervals

    # there should still be no diff or missing intervals in 3 days time
    assert new_seed_snapshot.model.interval_unit.is_day
    with time_machine.travel(timedelta(days=3)):
        ctx.clear_caches()
        ctx.load()
        plan = ctx.plan(auto_apply=True)
        assert not plan.has_changes
        assert not plan.missing_intervals

    # change seed data
    seed_path.write_text("\n".join(["id,name", "1,test", "2,updated"]))

    # new plan - NOW we should backfill because data changed
    ctx.load()
    plan = ctx.plan(auto_apply=True)
    assert plan.has_changes

    updated_seed_snapshot = ctx.snapshots['"memory"."test"."source_data"']

    assert (
        updated_seed_snapshot.snapshot_id
        != new_seed_snapshot.snapshot_id
        != original_seed_snapshot.snapshot_id
    )
    assert not updated_seed_snapshot.forward_only
    assert plan.directly_modified == {updated_seed_snapshot.snapshot_id}
    assert plan.metadata_updated == set()
    assert plan.missing_intervals

    # prove backfilled data loaded
    assert ctx.engine_adapter.fetchall("select id, name from memory.test.source_data") == [
        (1, "test"),
        (2, "updated"),
    ]


@time_machine.travel("2023-01-08 15:00:00 UTC")
def test_seed_model_promote_to_prod_after_dev(
    init_and_plan_context: t.Callable,
):
    context, plan = init_and_plan_context("examples/sushi")
    context.apply(plan)

    with open(context.path / "seeds" / "waiter_names.csv", "a") as f:
        f.write("\n10,New Waiter")

    context.load()

    waiter_names_snapshot = context.get_snapshot("sushi.waiter_names")
    plan = context.plan("dev", skip_tests=True, auto_apply=True, no_prompts=True)
    assert waiter_names_snapshot.snapshot_id in plan.directly_modified

    # Trigger a metadata change to reuse the previous version
    waiter_names_model = waiter_names_snapshot.model.copy(
        update={"description": "Updated description"}
    )
    context.upsert_model(waiter_names_model)
    context.plan("dev", skip_tests=True, auto_apply=True, no_prompts=True)

    # Promote all changes to prod
    waiter_names_snapshot = context.get_snapshot("sushi.waiter_names")
    plan = context.plan_builder("prod", skip_tests=True).build()
    # Clear the cache to source the dehydrated model instance from the state
    context.clear_caches()
    context.apply(plan)

    assert (
        context.engine_adapter.fetchone("SELECT COUNT(*) FROM sushi.waiter_names WHERE id = 10")[0]
        == 1
    )
