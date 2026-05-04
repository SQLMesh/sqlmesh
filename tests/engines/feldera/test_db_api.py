import types

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