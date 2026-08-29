import typing as t
from unittest import mock

import pytest
from pathlib import Path
from sqlglot import exp
from sqlglot.optimizer.qualify_columns import quote_identifiers
from sqlglot.helper import seq_get
from sqlmesh.cli.project_init import ProjectTemplate, init_example_project
from sqlmesh.core.config import Config
from sqlmesh.core.engine_adapter import BigQueryEngineAdapter
from sqlmesh.core.engine_adapter.mixins import (
    TableAlterDropClusterKeyOperation,
    TableAlterChangeClusterKeyOperation,
)
from sqlmesh.core.engine_adapter.shared import DataObject
import sqlmesh.core.dialect as d
from sqlmesh.core.model import SqlModel, load_sql_based_model
from sqlmesh.core.plan import Plan, BuiltInPlanEvaluator
from sqlmesh.core.table_diff import TableDiff
from sqlmesh.utils import CorrelationId
from tests.core.engine_adapter.integration import TestContext
from pytest import FixtureRequest
from tests.core.engine_adapter.integration import (
    TestContext,
    generate_pytest_params,
    ENGINES_BY_NAME,
    IntegrationTestEngine,
)


@pytest.fixture(params=list(generate_pytest_params(ENGINES_BY_NAME["bigquery"])))
def ctx(
    request: FixtureRequest,
    create_test_context: t.Callable[[IntegrationTestEngine, str, str], t.Iterable[TestContext]],
) -> t.Iterable[TestContext]:
    yield from create_test_context(*request.param)


@pytest.fixture
def engine_adapter(ctx: TestContext) -> BigQueryEngineAdapter:
    assert isinstance(ctx.engine_adapter, BigQueryEngineAdapter)
    return ctx.engine_adapter


def test_get_alter_expressions_includes_clustering(
    ctx: TestContext, engine_adapter: BigQueryEngineAdapter
):
    clustered_table = ctx.table("clustered_table")
    clustered_differently_table = ctx.table("clustered_differently_table")
    normal_table = ctx.table("normal_table")

    engine_adapter.execute(
        f"CREATE TABLE {clustered_table.sql(dialect=ctx.dialect)} (c1 int, c2 timestamp) CLUSTER BY c1"
    )
    engine_adapter.execute(
        f"CREATE TABLE {clustered_differently_table.sql(dialect=ctx.dialect)} (c1 int, c2 timestamp) CLUSTER BY c1, c2"
    )
    engine_adapter.execute(
        f"CREATE TABLE {normal_table.sql(dialect=ctx.dialect)} (c1 int, c2 timestamp)"
    )

    metadata = engine_adapter.get_data_objects(
        normal_table.db, {clustered_table.name, clustered_differently_table.name, normal_table.name}
    )
    clustered_table_metadata = next(md for md in metadata if md.name == clustered_table.name)
    clustered_differently_table_metadata = next(
        md for md in metadata if md.name == clustered_differently_table.name
    )
    normal_table_metadata = next(md for md in metadata if md.name == normal_table.name)

    assert clustered_table_metadata.clustering_key == "(c1)"
    assert clustered_differently_table_metadata.clustering_key == "(c1,c2)"
    assert normal_table_metadata.clustering_key is None

    assert len(engine_adapter.get_alter_operations(normal_table, normal_table)) == 0
    assert len(engine_adapter.get_alter_operations(clustered_table, clustered_table)) == 0

    # alter table drop clustered
    clustered_to_normal = engine_adapter.get_alter_operations(clustered_table, normal_table)
    assert len(clustered_to_normal) == 1
    assert isinstance(clustered_to_normal[0], TableAlterDropClusterKeyOperation)
    assert clustered_to_normal[0].target_table == clustered_table
    assert not hasattr(clustered_to_normal[0], "clustering_key")

    # alter table add clustered
    normal_to_clustered = engine_adapter.get_alter_operations(normal_table, clustered_table)
    assert len(normal_to_clustered) == 1
    operation = normal_to_clustered[0]
    assert isinstance(operation, TableAlterChangeClusterKeyOperation)
    assert operation.target_table == normal_table
    assert operation.clustering_key == "(c1)"

    # alter table change clustering (c1 -> (c1, c2))
    clustered_to_clustered_differently = engine_adapter.get_alter_operations(
        clustered_table, clustered_differently_table
    )
    assert len(clustered_to_clustered_differently) == 1
    operation = clustered_to_clustered_differently[0]
    assert isinstance(operation, TableAlterChangeClusterKeyOperation)
    assert operation.target_table == clustered_table
    assert operation.clustering_key == "(c1,c2)"

    # alter table change clustering ((c1, c2) -> c1)
    clustered_differently_to_clustered = engine_adapter.get_alter_operations(
        clustered_differently_table, clustered_table
    )
    assert len(clustered_differently_to_clustered) == 1
    operation = clustered_differently_to_clustered[0]
    assert isinstance(operation, TableAlterChangeClusterKeyOperation)
    assert operation.target_table == clustered_differently_table
    assert operation.clustering_key == "(c1)"


def test_mutating_clustered_by_forward_only(
    ctx: TestContext, engine_adapter: BigQueryEngineAdapter
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
                    name {model_name.sql(dialect="bigquery")},
                    kind INCREMENTAL_BY_TIME_RANGE (
                        time_column partitiondate
                    ),
                    {extra_props}
                    start '2021-01-01',
                    cron '@daily',
                    dialect 'bigquery'
                );

                select 1 as ID, current_date() as partitiondate
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
    m2 = _create_model(clustered_by="partitiondate")
    m3 = _create_model(clustered_by="(id, partitiondate)")

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
    assert metadata.clustering_key == "(partitiondate)"

    # Next plan - change clustering key (clustered -> clustered differently)
    sqlmesh.upsert_model(m3)
    plan_3: Plan = sqlmesh.plan(auto_apply=True, no_prompts=True, forward_only=True)
    assert len(plan_3.snapshots) == 1
    target_table_3 = exp.to_table(list(plan_3.snapshots.values())[0].table_name())
    quote_identifiers(target_table_3)

    assert target_table_1 == target_table_3

    metadata = _get_data_object(target_table_1)
    assert metadata.is_clustered
    assert metadata.clustering_key == "(id,partitiondate)"

    # Next plan - drop clustering key
    sqlmesh.upsert_model(m1)
    plan_4: Plan = sqlmesh.plan(auto_apply=True, no_prompts=True, forward_only=True)
    assert len(plan_4.snapshots) == 1
    target_table_4 = exp.to_table(list(plan_4.snapshots.values())[0].table_name())
    quote_identifiers(target_table_4)

    assert target_table_1 == target_table_4

    metadata = _get_data_object(target_table_1)
    assert not metadata.is_clustered


def test_information_schema_view_external_model(ctx: TestContext, tmp_path: Path):
    # Information schema views are represented as:
    #
    # Table(
    #   this=Identifier(INFORMATION_SCHEMA.SOME_VIEW, quoted=True),
    #   db=Identifier(some_schema),
    #   catalog=Identifier(some_catalog))
    #
    # This representation is produced by BigQuery's parser, so that the mapping schema
    # nesting depth is consistent with other table references in a project, which will
    # usually look like `project.dataset.table`.
    information_schema_tables = ctx.table("INFORMATION_SCHEMA.TABLES")
    assert len(information_schema_tables.parts) == 3

    model_name = ctx.table("test")
    dependency = f"`{'.'.join(part.name for part in information_schema_tables.parts)}`"

    init_example_project(tmp_path, engine_type="bigquery", template=ProjectTemplate.EMPTY)
    with open(tmp_path / "models" / "test.sql", "w", encoding="utf-8") as f:
        f.write(
            f"""
            MODEL (
              name {model_name.sql("bigquery")},
              kind FULL,
              dialect 'bigquery'
            );

            SELECT * FROM {dependency} AS tables
            """
        )

    def _mutate_config(_: str, config: Config) -> None:
        config.model_defaults.dialect = "bigquery"

    sqlmesh = ctx.create_context(_mutate_config, path=tmp_path)
    sqlmesh.create_external_models()
    sqlmesh.load()

    actual_columns_to_types = sqlmesh.get_model(information_schema_tables.sql()).columns_to_types
    expected_columns_to_types = {
        "table_catalog": exp.DataType.build("TEXT"),
        "table_schema": exp.DataType.build("TEXT"),
        "table_name": exp.DataType.build("TEXT"),
        "table_type": exp.DataType.build("TEXT"),
    }

    assert actual_columns_to_types is not None
    assert actual_columns_to_types.items() >= expected_columns_to_types.items()

    rendered_query = sqlmesh.get_model(model_name.sql()).render_query()
    assert isinstance(rendered_query, exp.Query)
    assert not rendered_query.selects[0].is_star


def test_compare_nested_values_in_table_diff(ctx: TestContext):
    src_table = ctx.table("source")
    target_table = ctx.table("target")

    query: exp.Query = exp.maybe_parse(
        """
        SELECT
          1 AS id,
          STRUCT(
            'Main String' AS top_level_string,
            [1, 2, 3, 4] AS top_level_array,
            STRUCT(
              'Nested String' AS nested_string,
              [STRUCT(
                'Inner Struct String 1' AS inner_string,
                [10, 20, 30] AS inner_array
              ), STRUCT(
                'Inner Struct String 2' AS inner_string,
                [40, 50, 60] AS inner_array
              )] AS nested_array_of_structs
            ) AS nested_struct,
            [STRUCT(
              'Array Struct String 1' AS array_struct_string,
              STRUCT(
                'Deeper Nested String' AS deeper_nested_string,
                [100, 200] AS deeper_nested_array
              ) AS deeper_nested_struct
            ), STRUCT(
              'Array Struct String 2' AS array_struct_string,
              STRUCT(
                'Another Nested String' AS deeper_nested_string,
                [300, 400] AS deeper_nested_array
              ) AS deeper_nested_struct
            )] AS array_of_structs_with_nested_structs,
            ARRAY(
              SELECT STRUCT(
                CONCAT('Dynamic String ', CAST(num AS STRING)) AS dynamic_string,
                ARRAY(
                  SELECT CAST(num * multiplier AS INT64)
                  FROM UNNEST([1, 2, 3]) AS multiplier
                ) AS dynamic_array
              )
              FROM UNNEST([1, 2, 3]) AS num
            ) AS dynamically_generated_struct_array
          ) AS nested_value
        """,
        dialect="bigquery",
    )

    ctx.engine_adapter.ctas(src_table, query)
    ctx.engine_adapter.ctas(target_table, query)

    table_diff = TableDiff(
        adapter=ctx.engine_adapter,
        source=exp.table_name(src_table),
        target=exp.table_name(target_table),
        on=["id"],
    )
    row_diff = table_diff.row_diff()

    assert row_diff.stats["join_count"] == 1
    assert row_diff.full_match_count == 1

    ctx.engine_adapter.drop_table(src_table)
    ctx.engine_adapter.drop_table(target_table)

    query1: exp.Query = exp.maybe_parse(
        "SELECT 0 as id, [STRUCT(0 as struct_id, 'value1' as struct_value), STRUCT(1 as struct_id, 'value2' as struct_value)] as struct_array",
        dialect="bigquery",
    )
    query2: exp.Query = exp.maybe_parse(
        "SELECT 0 as id, [STRUCT(0 as struct_id, 'value2' as struct_value), STRUCT(1 as struct_id, 'value1' as struct_value)] as struct_array",
        dialect="bigquery",
    )

    ctx.engine_adapter.ctas(src_table, query1)
    ctx.engine_adapter.ctas(target_table, query2)

    table_diff = TableDiff(
        adapter=ctx.engine_adapter,
        source=exp.table_name(src_table),
        target=exp.table_name(target_table),
        on=["id"],
    )
    row_diff = table_diff.row_diff()

    assert row_diff.stats["join_count"] == 1
    assert row_diff.full_match_count == 0

    ctx.engine_adapter.drop_table(src_table)
    ctx.engine_adapter.drop_table(target_table)


def test_get_bq_schema(ctx: TestContext, engine_adapter: BigQueryEngineAdapter):
    from google.cloud.bigquery import SchemaField

    table = ctx.table("test")

    engine_adapter.execute(f"""
    CREATE TABLE {table.sql(dialect=ctx.dialect)} (
      id STRING NOT NULL,
      user_data STRUCT<id STRING NOT NULL, name STRING NOT NULL, address STRING>,
      tags ARRAY<STRING>,
      score NUMERIC,
      created_at DATETIME
    )
    """)

    bg_schema = engine_adapter.get_bq_schema(table)
    assert len(bg_schema) == 5
    assert bg_schema[0] == SchemaField(name="id", field_type="STRING", mode="REQUIRED")
    assert bg_schema[1] == SchemaField(
        name="user_data",
        field_type="RECORD",
        mode="NULLABLE",
        fields=[
            SchemaField(name="id", field_type="STRING", mode="REQUIRED"),
            SchemaField(name="name", field_type="STRING", mode="REQUIRED"),
            SchemaField(name="address", field_type="STRING", mode="NULLABLE"),
        ],
    )
    assert bg_schema[2] == SchemaField(name="tags", field_type="STRING", mode="REPEATED")
    assert bg_schema[3] == SchemaField(name="score", field_type="NUMERIC", mode="NULLABLE")
    assert bg_schema[4] == SchemaField(name="created_at", field_type="DATETIME", mode="NULLABLE")


def test_column_types(ctx: TestContext):
    model_name = ctx.table("test")
    sqlmesh = ctx.create_context()

    sqlmesh.upsert_model(
        load_sql_based_model(
            d.parse(
                f"""
                MODEL (
                    name {model_name},
                );

                SELECT
                    RANGE('01-01-1900'::DATE, '01-01-1902'::DATE) AS col1,
                    JSON '{{"id": 10}}' AS col2,
                    STRUCT([PARSE_JSON('{{"id": 10}}')] AS arr) AS col3;
                """
            )
        )
    )

    sqlmesh.plan(auto_apply=True, no_prompts=True)

    columns = sqlmesh.engine_adapter.columns(model_name)

    assert columns["col1"].is_type("RANGE<DATE>")
    assert columns["col2"].is_type("JSON")

    col3 = columns["col3"]
    coldef = col3.find(exp.ColumnDef)
    assert col3.is_type("STRUCT")
    assert coldef and coldef.kind and coldef.kind.is_type("ARRAY<JSON>")


def test_table_diff_table_name_matches_column_name(ctx: TestContext):
    src_table = ctx.table("source")
    target_table = ctx.table("target")

    # BigQuery has a quirk where if you do SELECT foo FROM project-id.schema.foo, the projection is
    # interpreted as a struct column, reflecting the scanned table's schema, even if the table has
    # a column with the same name (foo).
    #
    # This is a problem, because we compare the columns of the source and target tables using the
    # equality operator (=), which is not defined for struct values in BigQuery, leading to an error.
    query: exp.Query = exp.maybe_parse("SELECT 1 AS s, 2 AS source, 3 AS target")

    ctx.engine_adapter.ctas(src_table, query)
    ctx.engine_adapter.ctas(target_table, query)

    table_diff = TableDiff(
        adapter=ctx.engine_adapter,
        source=exp.table_name(src_table),
        target=exp.table_name(target_table),
        on=["s"],
    )

    row_diff = table_diff.row_diff()

    assert row_diff.stats["join_count"] == 1
    assert row_diff.full_match_count == 1


def test_plan_correlation_id_in_job_labels(ctx: TestContext):
    model_name = ctx.table("test")

    sqlmesh = ctx.create_context()
    sqlmesh.upsert_model(
        load_sql_based_model(d.parse(f"MODEL (name {model_name}, kind FULL); SELECT 1 AS col"))
    )

    # Create a plan evaluator and a plan to evaluate
    plan_evaluator = BuiltInPlanEvaluator(
        sqlmesh.state_sync,
        sqlmesh.snapshot_evaluator,
        sqlmesh.create_scheduler,
        sqlmesh.default_catalog,
    )
    plan: Plan = sqlmesh.plan_builder("prod", skip_tests=True).build()

    # Evaluate the plan and retrieve the plan evaluator's adapter
    plan_evaluator.evaluate(plan.to_evaluatable())
    adapter = t.cast(BigQueryEngineAdapter, plan_evaluator.snapshot_evaluator.adapter)

    # Case 1: Ensure that the correlation id is set in the underlying adapter
    assert adapter.correlation_id is not None

    # Case 2: Ensure that the correlation id is set in the job labels
    labels = adapter._job_params.get("labels")
    correlation_id = CorrelationId.from_plan_id(plan.plan_id)
    assert labels == {correlation_id.job_type.value.lower(): correlation_id.job_id}


def test_run_correlation_id_in_job_labels(ctx: TestContext):
    run_id = "test_run_id"
    model_name = ctx.table("run_test")

    sqlmesh = ctx.create_context()
    sqlmesh.upsert_model(
        load_sql_based_model(
            d.parse(
                f"""
MODEL (
    name {model_name},
    kind INCREMENTAL_BY_TIME_RANGE (
        time_column event_ts
    ),
    cron '@daily',
    start '2023-01-07'
);
SELECT 1 AS col, '2023-01-07' AS event_ts
"""
            )
        )
    )
    sqlmesh.plan(auto_apply=True, no_prompts=True, execution_time="2023-01-08 15:00:00")

    captured_evaluators: t.List = []
    original_scheduler = sqlmesh.scheduler

    def scheduler_wrapper(
        environment: t.Optional[str] = None,
        snapshot_evaluator: t.Optional[t.Any] = None,
    ):
        if snapshot_evaluator is not None:
            captured_evaluators.append(snapshot_evaluator)
        return original_scheduler(environment=environment, snapshot_evaluator=snapshot_evaluator)

    with mock.patch("sqlmesh.core.context.analytics.collector.on_run_start", return_value=run_id):
        with mock.patch.object(sqlmesh, "scheduler", scheduler_wrapper):
            sqlmesh.run(execution_time="2023-01-09 00:00:00")

    assert captured_evaluators
    adapter = t.cast(BigQueryEngineAdapter, captured_evaluators[-1].adapter)

    assert adapter.correlation_id is not None
    labels = adapter._job_params.get("labels")
    correlation_id = CorrelationId.from_run_id(run_id)
    assert labels == {correlation_id.job_type.value.lower(): correlation_id.job_id}
