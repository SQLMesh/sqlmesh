from __future__ import annotations

from unittest.mock import patch
from textwrap import dedent
import os
import pytest
from pathlib import Path
from sqlmesh.core.console import (
    get_console,
)
from sqlmesh.core.config.naming import NameInferenceConfig
from sqlmesh.core.model.common import ParsableSql
from sqlmesh.utils.concurrency import NodeExecutionFailedError

from sqlmesh.core import constants as c
from sqlmesh.core.config import (
    Config,
    GatewayConfig,
    ModelDefaultsConfig,
    DuckDBConnectionConfig,
)
from sqlmesh.core.console import get_console
from sqlmesh.core.context import Context
from sqlmesh.utils import yaml
from sqlmesh.utils.date import now
from tests.conftest import DuckDBMetadata
from tests.utils.test_helpers import use_terminal_console
from tests.core.integration.utils import validate_apply_basics


pytestmark = pytest.mark.slow


@use_terminal_console
def test_multi(mocker):
    context = Context(paths=["examples/multi/repo_1", "examples/multi/repo_2"], gateway="memory")

    with patch.object(get_console(), "log_warning") as mock_logger:
        context.plan_builder(environment="dev")
        warnings = mock_logger.call_args[0][0]
        repo1_path, repo2_path = context.configs.keys()
        assert f"Linter warnings for {repo1_path}" in warnings
        assert f"Linter warnings for {repo2_path}" not in warnings

    assert (
        context.render("bronze.a").sql()
        == '''SELECT 1 AS "col_a", 'b' AS "col_b", 1 AS "one", 'repo_1' AS "dup"'''
    )
    assert (
        context.render("silver.d").sql()
        == '''SELECT "c"."col_a" AS "col_a", 2 AS "two", 'repo_2' AS "dup" FROM "memory"."silver"."c" AS "c"'''
    )
    context._new_state_sync().reset(default_catalog=context.default_catalog)
    plan = context.plan_builder().build()
    assert len(plan.new_snapshots) == 5
    context.apply(plan)

    # Ensure before_all, after_all statements for multiple repos have executed
    environment_statements = context.state_reader.get_environment_statements(c.PROD)
    assert len(environment_statements) == 2
    assert context.fetchdf("select * from before_1").to_dict()["1"][0] == 1
    assert context.fetchdf("select * from before_2").to_dict()["2"][0] == 2
    assert context.fetchdf("select * from after_1").to_dict()["repo_1"][0] == "repo_1"
    assert context.fetchdf("select * from after_2").to_dict()["repo_2"][0] == "repo_2"

    old_context = context
    context = Context(
        paths=["examples/multi/repo_1"],
        state_sync=old_context.state_sync,
        gateway="memory",
    )
    context._engine_adapter = old_context.engine_adapter
    del context.engine_adapters

    model = context.get_model("bronze.a")
    assert model.project == "repo_1"
    context.upsert_model(
        model.copy(
            update={
                "query_": ParsableSql(sql=model.query.select("'c' AS c").sql(dialect=model.dialect))
            }
        )
    )
    plan = context.plan_builder().build()

    assert set(snapshot.name for snapshot in plan.directly_modified) == {
        '"memory"."bronze"."a"',
        '"memory"."bronze"."b"',
        '"memory"."silver"."e"',
    }
    assert sorted([x.name for x in list(plan.indirectly_modified.values())[0]]) == [
        '"memory"."silver"."c"',
        '"memory"."silver"."d"',
    ]
    assert len(plan.missing_intervals) == 3
    context.apply(plan)
    validate_apply_basics(context, c.PROD, plan.snapshots.values())

    # Ensure that before_all and after_all statements of both repos are there despite planning with repo_1
    environment_statements = context.state_reader.get_environment_statements(c.PROD)
    assert len(environment_statements) == 2

    # Ensure that environment statements have the project field set correctly
    sorted_env_statements = sorted(environment_statements, key=lambda es: es.project)
    assert sorted_env_statements[0].project == "repo_1"
    assert sorted_env_statements[1].project == "repo_2"

    # Assert before_all and after_all for each project
    assert sorted_env_statements[0].before_all == [
        "CREATE TABLE IF NOT EXISTS before_1 AS select @one()"
    ]
    assert sorted_env_statements[0].after_all == [
        "CREATE TABLE IF NOT EXISTS after_1 AS select @dup()"
    ]
    assert sorted_env_statements[1].before_all == [
        "CREATE TABLE IF NOT EXISTS before_2 AS select @two()"
    ]
    assert sorted_env_statements[1].after_all == [
        "CREATE TABLE IF NOT EXISTS after_2 AS select @dup()"
    ]


@use_terminal_console
def test_multi_repo_single_project_environment_statements_update(copy_to_temp_path):
    paths = copy_to_temp_path("examples/multi")
    repo_1_path = f"{paths[0]}/repo_1"
    repo_2_path = f"{paths[0]}/repo_2"

    context = Context(paths=[repo_1_path, repo_2_path], gateway="memory")
    context._new_state_sync().reset(default_catalog=context.default_catalog)

    initial_plan = context.plan_builder().build()
    context.apply(initial_plan)

    # Get initial statements
    initial_statements = context.state_reader.get_environment_statements(c.PROD)
    assert len(initial_statements) == 2

    # Modify repo_1's config to add a new before_all statement
    repo_1_config_path = f"{repo_1_path}/config.yaml"
    with open(repo_1_config_path, "r") as f:
        config_content = f.read()

    # Add a new before_all statement to repo_1 only
    modified_config = config_content.replace(
        "CREATE TABLE IF NOT EXISTS before_1 AS select @one()",
        "CREATE TABLE IF NOT EXISTS before_1 AS select @one()\n  - CREATE TABLE IF NOT EXISTS before_1_modified AS select 999",
    )

    with open(repo_1_config_path, "w") as f:
        f.write(modified_config)

    # Create new context with modified config but only for repo_1
    context_repo_1_only = Context(
        paths=[repo_1_path], state_sync=context.state_sync, gateway="memory"
    )

    # Plan with only repo_1, this should preserve repo_2's statements from state
    repo_1_plan = context_repo_1_only.plan_builder(environment="dev").build()
    context_repo_1_only.apply(repo_1_plan)
    updated_statements = context_repo_1_only.state_reader.get_environment_statements("dev")

    # Should still have statements from both projects
    assert len(updated_statements) == 2

    # Sort by project
    sorted_updated = sorted(updated_statements, key=lambda es: es.project or "")

    # Verify repo_1 has the new statement
    repo_1_updated = sorted_updated[0]
    assert repo_1_updated.project == "repo_1"
    assert len(repo_1_updated.before_all) == 2
    assert "CREATE TABLE IF NOT EXISTS before_1_modified" in repo_1_updated.before_all[1]

    # Verify repo_2 statements are preserved from state
    repo_2_preserved = sorted_updated[1]
    assert repo_2_preserved.project == "repo_2"
    assert len(repo_2_preserved.before_all) == 1
    assert "CREATE TABLE IF NOT EXISTS before_2" in repo_2_preserved.before_all[0]
    assert "CREATE TABLE IF NOT EXISTS after_2 AS select @dup()" in repo_2_preserved.after_all[0]


@use_terminal_console
def test_multi_virtual_layer(copy_to_temp_path):
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
    assert context.default_catalog_per_gateway == {"first": "db_1", "second": "db_2"}
    assert len(context.engine_adapters) == 2

    # For the model without gateway the default should be used and the gateway variable should overide the global
    assert (
        context.render("first_schema.model_one").sql()
        == 'SELECT \'gateway_1\' AS "item_id", 88 AS "global_one", 1 AS "macro_one"'
    )

    # For model with gateway specified the appropriate variable should be used to overide
    assert (
        context.render("db_2.second_schema.model_one").sql()
        == 'SELECT \'gateway_2\' AS "item_id", 88 AS "global_one", 1 AS "macro_one"'
    )

    plan = context.plan_builder().build()
    assert len(plan.new_snapshots) == 4
    context.apply(plan)

    # Validate the tables that source from the first tables are correct as well with evaluate
    assert (
        context.evaluate(
            "first_schema.model_two", start=now(), end=now(), execution_time=now()
        ).to_string()
        == "     item_id  global_one\n0  gateway_1          88"
    )
    assert (
        context.evaluate(
            "db_2.second_schema.model_two", start=now(), end=now(), execution_time=now()
        ).to_string()
        == "     item_id  global_one\n0  gateway_2          88"
    )

    assert sorted(set(snapshot.name for snapshot in plan.directly_modified)) == [
        '"db_1"."first_schema"."model_one"',
        '"db_1"."first_schema"."model_two"',
        '"db_2"."second_schema"."model_one"',
        '"db_2"."second_schema"."model_two"',
    ]

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

    assert state_environments[0].gateway_managed
    assert len(state_snapshots) == len(state_environments[0].snapshots)
    assert [snapshot.name for snapshot in plan.directly_modified] == [
        '"db_1"."first_schema"."model_one"'
    ]
    assert [x.name for x in list(plan.indirectly_modified.values())[0]] == [
        '"db_1"."first_schema"."model_two"'
    ]

    assert len(plan.missing_intervals) == 1
    assert (
        context.evaluate(
            "db_1.first_schema.model_one", start=now(), end=now(), execution_time=now()
        ).to_string()
        == "     item_id  global_one  macro_one extra\n0  gateway_1          88          1     c"
    )

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

    metadata_engine_1 = DuckDBMetadata.from_context(context)
    start_schemas_1 = set(metadata_engine_1.schemas)
    assert sorted(start_schemas_1) == sorted(
        {"first_schema__dev", "sqlmesh", "first_schema", "sqlmesh__first_schema"}
    )

    metadata_engine_2 = DuckDBMetadata(context._get_engine_adapter("second"))
    start_schemas_2 = set(metadata_engine_2.schemas)
    assert sorted(start_schemas_2) == sorted(
        {"sqlmesh__second_schema", "second_schema", "second_schema__dev"}
    )

    # Invalidate dev environment
    context.invalidate_environment("dev")
    invalidate_environment = context.state_sync.get_environment("dev")
    assert invalidate_environment is not None
    assert invalidate_environment.expiration_ts < dev_environment.expiration_ts  # type: ignore
    assert sorted(start_schemas_1) == sorted(set(metadata_engine_1.schemas))
    assert sorted(start_schemas_2) == sorted(set(metadata_engine_2.schemas))

    # Run janitor
    context._run_janitor()
    assert context.state_sync.get_environment("dev") is None
    removed_schemas = start_schemas_1 - set(metadata_engine_1.schemas)
    assert removed_schemas == {"first_schema__dev"}
    removed_schemas = start_schemas_2 - set(metadata_engine_2.schemas)
    assert removed_schemas == {"second_schema__dev"}
    prod_environment = context.state_sync.get_environment("prod")

    # Remove the second gateway's second model and apply plan
    second_model = path / "models/second_schema/model_two.sql"
    os.remove(second_model)
    assert not second_model.exists()
    context = Context(paths=paths, config=config)
    plan = context.plan_builder().build()
    context.apply(plan)
    prod_environment = context.state_sync.get_environment("prod")
    assert len(prod_environment.snapshots_) == 3

    # Changing the flag should show a diff
    context.config.gateway_managed_virtual_layer = False
    plan = context.plan_builder().build()
    assert not plan.requires_backfill
    assert (
        plan.context_diff.previous_gateway_managed_virtual_layer
        != plan.context_diff.gateway_managed_virtual_layer
    )
    assert plan.context_diff.has_changes

    # This should error since the default_gateway won't have access to create the view on a non-shared catalog
    with pytest.raises(NodeExecutionFailedError, match=r"Execution failed for node SnapshotId*"):
        context.apply(plan)


def test_multi_dbt(mocker):
    context = Context(paths=["examples/multi_dbt/bronze", "examples/multi_dbt/silver"])
    context._new_state_sync().reset(default_catalog=context.default_catalog)
    plan = context.plan_builder().build()
    assert len(plan.new_snapshots) == 4
    context.apply(plan)
    validate_apply_basics(context, c.PROD, plan.snapshots.values())

    environment_statements = context.state_sync.get_environment_statements(c.PROD)
    assert len(environment_statements) == 2
    bronze_statements = environment_statements[0]
    assert bronze_statements.before_all == [
        "JINJA_STATEMENT_BEGIN;\nCREATE TABLE IF NOT EXISTS analytic_stats (physical_table VARCHAR, evaluation_time VARCHAR);\nJINJA_END;"
    ]
    assert not bronze_statements.after_all
    silver_statements = environment_statements[1]
    assert not silver_statements.before_all
    assert silver_statements.after_all == [
        "JINJA_STATEMENT_BEGIN;\n{{ store_schemas(schemas) }}\nJINJA_END;"
    ]
    assert "store_schemas" in silver_statements.jinja_macros.root_macros
    analytics_table = context.fetchdf("select * from analytic_stats;")
    assert sorted(analytics_table.columns) == sorted(["physical_table", "evaluation_time"])
    schema_table = context.fetchdf("select * from schema_table;")
    assert sorted(schema_table.all_schemas[0]) == sorted(["bronze", "silver"])


def test_multi_hybrid(mocker):
    context = Context(
        paths=["examples/multi_hybrid/dbt_repo", "examples/multi_hybrid/sqlmesh_repo"]
    )
    context._new_state_sync().reset(default_catalog=context.default_catalog)
    plan = context.plan_builder().build()

    assert len(plan.new_snapshots) == 5
    assert context.dag.roots == {'"memory"."dbt_repo"."e"'}
    assert context.dag.graph['"memory"."dbt_repo"."c"'] == {'"memory"."sqlmesh_repo"."b"'}
    assert context.dag.graph['"memory"."sqlmesh_repo"."b"'] == {'"memory"."sqlmesh_repo"."a"'}
    assert context.dag.graph['"memory"."sqlmesh_repo"."a"'] == {'"memory"."dbt_repo"."e"'}
    assert context.dag.downstream('"memory"."dbt_repo"."e"') == [
        '"memory"."sqlmesh_repo"."a"',
        '"memory"."sqlmesh_repo"."b"',
        '"memory"."dbt_repo"."c"',
        '"memory"."dbt_repo"."d"',
    ]

    sqlmesh_model_a = context.get_model("sqlmesh_repo.a")
    dbt_model_c = context.get_model("dbt_repo.c")
    assert sqlmesh_model_a.project == "sqlmesh_repo"

    sqlmesh_rendered = (
        'SELECT "e"."col_a" AS "col_a", "e"."col_b" AS "col_b" FROM "memory"."dbt_repo"."e" AS "e"'
    )
    dbt_rendered = 'SELECT DISTINCT ROUND(CAST(("b"."col_a" / NULLIF(100, 0)) AS DECIMAL(16, 2)), 2) AS "rounded_col_a" FROM "memory"."sqlmesh_repo"."b" AS "b"'
    assert sqlmesh_model_a.render_query().sql() == sqlmesh_rendered
    assert dbt_model_c.render_query().sql() == dbt_rendered

    context.apply(plan)
    validate_apply_basics(context, c.PROD, plan.snapshots.values())


def test_multi_repo_no_project_to_project(copy_to_temp_path):
    paths = copy_to_temp_path("examples/multi")
    repo_1_path = f"{paths[0]}/repo_1"
    repo_1_config_path = f"{repo_1_path}/config.yaml"
    with open(repo_1_config_path, "r") as f:
        config_content = f.read()
    with open(repo_1_config_path, "w") as f:
        f.write(config_content.replace("project: repo_1\n", ""))

    context = Context(paths=[repo_1_path], gateway="memory")
    context._new_state_sync().reset(default_catalog=context.default_catalog)
    plan = context.plan_builder().build()
    context.apply(plan)

    # initially models in prod have no project
    prod_snapshots = context.state_reader.get_snapshots(
        context.state_reader.get_environment(c.PROD).snapshots
    )
    for snapshot in prod_snapshots.values():
        assert snapshot.node.project == ""

    # we now adopt multi project by adding a project name
    with open(repo_1_config_path, "r") as f:
        config_content = f.read()
    with open(repo_1_config_path, "w") as f:
        f.write("project: repo_1\n" + config_content)

    context_with_project = Context(
        paths=[repo_1_path],
        state_sync=context.state_sync,
        gateway="memory",
    )
    context_with_project._engine_adapter = context.engine_adapter
    del context_with_project.engine_adapters

    # local models should take precedence to pick up the new project name
    local_model_a = context_with_project.get_model("bronze.a")
    assert local_model_a.project == "repo_1"
    local_model_b = context_with_project.get_model("bronze.b")
    assert local_model_b.project == "repo_1"

    # also verify the plan works
    plan = context_with_project.plan_builder().build()
    context_with_project.apply(plan)
    validate_apply_basics(context_with_project, c.PROD, plan.snapshots.values())


def test_multi_repo_local_model_overrides_prod_from_other_project(copy_to_temp_path):
    paths = copy_to_temp_path("examples/multi")
    repo_1_path = f"{paths[0]}/repo_1"
    repo_2_path = f"{paths[0]}/repo_2"

    context = Context(paths=[repo_1_path, repo_2_path], gateway="memory")
    context._new_state_sync().reset(default_catalog=context.default_catalog)
    plan = context.plan_builder().build()
    assert len(plan.new_snapshots) == 5
    context.apply(plan)

    prod_model_c = context.get_model("silver.c")
    assert prod_model_c.project == "repo_2"

    with open(f"{repo_1_path}/models/c.sql", "w") as f:
        f.write(
            dedent("""\
            MODEL (
              name silver.c,
              kind FULL
            );

            SELECT DISTINCT col_a, col_b
            FROM bronze.a
        """)
        )

    # silver.c exists locally in repo 1 now AND in prod under repo_2
    context_repo1 = Context(
        paths=[repo_1_path],
        state_sync=context.state_sync,
        gateway="memory",
    )
    context_repo1._engine_adapter = context.engine_adapter
    del context_repo1.engine_adapters

    # local model should take precedence and its project should reflect the new project name
    local_model_c = context_repo1.get_model("silver.c")
    assert local_model_c.project == "repo_1"

    rendered = context_repo1.render("silver.c").sql()
    assert "col_b" in rendered

    # its downstream dependencies though should still be picked up
    plan = context_repo1.plan_builder().build()
    directly_modified_names = {snapshot.name for snapshot in plan.directly_modified}
    assert '"memory"."silver"."c"' in directly_modified_names
    assert '"memory"."silver"."d"' in directly_modified_names
    missing_interval_names = {s.snapshot_id.name for s in plan.missing_intervals}
    assert '"memory"."silver"."c"' in missing_interval_names
    assert '"memory"."silver"."d"' in missing_interval_names

    context_repo1.apply(plan)
    validate_apply_basics(context_repo1, c.PROD, plan.snapshots.values())
    result = context_repo1.fetchdf("SELECT * FROM memory.silver.c")
    assert "col_b" in result.columns


def test_engine_adapters_multi_repo_all_gateways_gathered(copy_to_temp_path):
    paths = copy_to_temp_path("examples/multi")
    repo_1_path = paths[0] / "repo_1"
    repo_2_path = paths[0] / "repo_2"

    # Add an extra gateway to repo_2's config
    repo_2_config_path = repo_2_path / "config.yaml"
    config_content = repo_2_config_path.read_text()

    modified_config = config_content.replace(
        "default_gateway: local",
        dedent("""
              extra:
                connection:
                  type: duckdb
                  database: extra.duckdb

            default_gateway: local
        """),
    )

    repo_2_config_path.write_text(modified_config)

    # Create context with both repos but using the repo_1 path first
    context = Context(
        paths=(repo_1_path, repo_2_path),
        gateway="memory",
    )

    # Verify all gateways from both repos are present
    gathered_gateways = context.engine_adapters.keys()
    expected_gateways = {"local", "memory", "extra"}
    assert gathered_gateways == expected_gateways


@use_terminal_console
def test_multi_repo_create_external_models(copy_to_temp_path):
    """create_external_models should not classify cross-repo models as external (sqlmesh#5326).

    silver.c and silver.e (repo_2) depend on bronze.a (repo_1). When running
    create_external_models with both repos loaded, bronze.a must NOT be treated
    as an external model because it is an internal model defined in repo_1.

    The observable symptom of the bug is a warning: "Unable to get schema for
    'bronze.a'" — because SQLMesh tries to query the schema of what it wrongly
    thinks is an external table.  A correct implementation never attempts this
    lookup and therefore emits no such warning.
    """
    paths = copy_to_temp_path("examples/multi")
    repo_1_path = f"{paths[0]}/repo_1"
    repo_2_path = f"{paths[0]}/repo_2"

    context = Context(paths=[repo_1_path, repo_2_path], gateway="memory")
    context._new_state_sync().reset(default_catalog=context.default_catalog)

    with patch.object(context.console, "log_warning") as mock_warning:
        context.create_external_models()

    warning_messages = [str(call) for call in mock_warning.call_args_list]
    schema_lookup_warnings = [
        msg
        for msg in warning_messages
        if "bronze" in msg and "a" in msg and "schema" in msg.lower()
    ]
    assert not schema_lookup_warnings, (
        "bronze.a should not be looked up as an external model, but got warnings: "
        + str(schema_lookup_warnings)
    )

    # repo_2's external_models.yaml must not contain bronze.a
    repo_2_external = Path(repo_2_path) / c.EXTERNAL_MODELS_YAML
    if repo_2_external.exists():
        contents = yaml.load(repo_2_external)
        external_names = [e["name"] for e in contents]
        assert not any("bronze" in name and "a" in name for name in external_names), (
            f"bronze.a should not be in repo_2's external models, but found: {external_names}"
        )

    # repo_1 has no external dependencies at all
    repo_1_external = Path(repo_1_path) / c.EXTERNAL_MODELS_YAML
    if repo_1_external.exists():
        contents = yaml.load(repo_1_external)
        assert len(contents) == 0, f"repo_1 should have no external models, got: {contents}"

    # Plan should still resolve all 5 models as internal after create_external_models
    context.load()
    plan = context.plan_builder().build()
    assert len(plan.new_snapshots) == 5
