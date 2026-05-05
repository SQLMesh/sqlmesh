import types

import pytest
from sqlglot import parse_one

from sqlmesh.engines.feldera import db_api


def test_classify_treats_comment_prefixed_create_schema_as_pipeline_ddl() -> None:
    assert (
        db_api._classify("/* sqlmesh */ CREATE SCHEMA foo")
        == db_api.SqlIntent.PIPELINE_DDL
    )


def test_cursor_defers_pipeline_deploy_until_non_ddl_statement() -> None:
    class FakeStateManager:
        def __init__(self) -> None:
            self.pending = False
            self.deploy_calls = 0
            self.pipeline = types.SimpleNamespace(query=lambda sql: [{"a": 1}])

        def register_ddl(self, sql: str) -> None:
            self.pending = True

        def has_pending_changes(self) -> bool:
            return self.pending

        def deploy(self, *args: object, **kwargs: object) -> object:
            self.deploy_calls += 1
            self.pending = False
            return self.pipeline

        def current_pipeline(self) -> object:
            return self.pipeline

        def queryable_relation_names(self) -> set[str]:
            return set()

    state_manager = FakeStateManager()
    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=state_manager,
    )

    cursor.execute("/* sqlmesh */ CREATE TABLE foo (id INT)")

    assert state_manager.deploy_calls == 0

    cursor.execute("SELECT 1 AS a")

    assert state_manager.deploy_calls == 1
    assert cursor.fetchall() == [(1,)]


def test_hydrate_existing_program_skips_empty_parse_results(monkeypatch) -> None:
    manager = db_api.PipelineStateManager()

    monkeypatch.setattr(db_api, "parse", lambda sql: [None, parse_one("CREATE TABLE foo (id INT)")])

    pipeline_module = types.ModuleType("feldera.pipeline")
    pipeline_module.Pipeline = type(
        "Pipeline",
        (),
        {
            "get": staticmethod(
                lambda pipeline_name, client: types.SimpleNamespace(
                    _inner=types.SimpleNamespace(program_code="ignored")
                )
            )
        },
    )
    feldera_module = types.ModuleType("feldera")
    feldera_module.pipeline = pipeline_module

    monkeypatch.setitem(__import__("sys").modules, "feldera", feldera_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.pipeline", pipeline_module)

    manager._hydrate_existing_program(object(), "test_pipeline")

    assert manager.pending_tables() == {"foo"}


def test_state_manager_tracks_view_materialization() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl("CREATE VIEW regular_view AS SELECT 1")
    manager.register_ddl("CREATE MATERIALIZED VIEW materialized_view AS SELECT 1")

    assert manager.pending_views() == {"regular_view", "materialized_view"}
    assert manager.is_materialized_view("regular_view") is False
    assert manager.is_materialized_view("materialized_view") is True


def test_state_manager_rewrites_table_ctas() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl(
        'CREATE TABLE "seed_model" AS SELECT CAST("id" AS INTEGER) AS "id" FROM (VALUES (1)) AS "t"("id")'
    )

    assert manager.assemble_program() == (
        'CREATE TABLE "seed_model" ("id" INT);\n'
        '\n'
        'CREATE MATERIALIZED VIEW "__sqlmesh_query__seed_model" AS SELECT * FROM "seed_model";\n'
        '\n'
        'INSERT INTO "seed_model" (id) SELECT CAST("id" AS INT) AS "id" FROM (VALUES (1)) AS "t"("id");'
    )


def test_state_manager_adds_query_mirrors_for_non_materialized_relations() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl('CREATE TABLE "full_model" ("id" INT)')
    manager.register_ddl('CREATE VIEW "view_model" AS SELECT "id" FROM "full_model"')
    manager.register_ddl(
        'CREATE MATERIALIZED VIEW "materialized_view_model" AS SELECT "id" FROM "full_model"'
    )

    program = manager.assemble_program()

    assert 'CREATE MATERIALIZED VIEW "__sqlmesh_query__full_model" AS SELECT * FROM "full_model";' in program
    assert 'CREATE MATERIALIZED VIEW "__sqlmesh_query__view_model" AS SELECT * FROM "view_model";' in program
    assert 'CREATE MATERIALIZED VIEW "__sqlmesh_query__materialized_view_model"' not in program


def test_hydrate_existing_program_skips_query_mirrors(monkeypatch) -> None:
    manager = db_api.PipelineStateManager()

    pipeline_module = types.ModuleType("feldera.pipeline")
    pipeline_module.Pipeline = type(
        "Pipeline",
        (),
        {
            "get": staticmethod(
                lambda pipeline_name, client: types.SimpleNamespace(
                    _inner=types.SimpleNamespace(
                        program_code=(
                            'CREATE TABLE "full_model" ("id" INT);\n'
                            'CREATE MATERIALIZED VIEW "__sqlmesh_query__full_model" AS SELECT * FROM "full_model";\n'
                            'CREATE VIEW "view_model" AS SELECT "id" FROM "full_model";\n'
                            'CREATE MATERIALIZED VIEW "__sqlmesh_query__view_model" AS SELECT * FROM "view_model";'
                        )
                    )
                )
            )
        },
    )
    feldera_module = types.ModuleType("feldera")
    feldera_module.pipeline = pipeline_module

    monkeypatch.setitem(__import__("sys").modules, "feldera", feldera_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.pipeline", pipeline_module)

    manager._hydrate_existing_program(object(), "test_pipeline")

    assert manager.pending_tables() == {"full_model"}
    assert manager.pending_views() == {"view_model"}


def test_cursor_rewrites_queries_to_query_mirrors() -> None:
    captured_queries = []

    class FakeStateManager:
        def has_pending_changes(self) -> bool:
            return False

        def queryable_relation_names(self) -> set[str]:
            return {"full_model", "view_model"}

        def current_pipeline(self) -> object:
            return types.SimpleNamespace(
                query=lambda sql: captured_queries.append(sql) or [{"count": 1}]
            )

    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=FakeStateManager(),
    )

    cursor.execute('SELECT COUNT(*) FROM "full_model"')

    assert parse_one(captured_queries[0]).sql() == parse_one(
        'SELECT COUNT(*) FROM "__sqlmesh_query__full_model"'
    ).sql()
    assert cursor.fetchone() == (1,)


def test_cursor_raises_execution_error_from_query_rows() -> None:
    class FakeStateManager:
        def has_pending_changes(self) -> bool:
            return False

        def queryable_relation_names(self) -> set[str]:
            return set()

        def current_pipeline(self) -> object:
            return types.SimpleNamespace(
                query=lambda sql: [{"COUNT(*)": "Execution error: test failure"}]
            )

    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=FakeStateManager(),
    )

    with pytest.raises(RuntimeError, match="Execution error: test failure"):
        cursor.execute("SELECT COUNT(*) FROM full_model")