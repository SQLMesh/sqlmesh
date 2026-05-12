import types
import typing as t

import pytest
from sqlglot import parse_one

from sqlmesh.engines.feldera import db_api


def test_classify_treats_comment_prefixed_create_schema_as_pipeline_ddl() -> None:
    assert db_api._classify("/* sqlmesh */ CREATE SCHEMA foo") == db_api.SqlIntent.PIPELINE_DDL


def test_is_virtual_layer_ddl_identifies_environment_alias_view() -> None:
    assert db_api._is_virtual_layer_ddl(
        'CREATE VIEW "analytics__dev"."source_events" AS '
        'SELECT * FROM "sqlmesh__analytics"."analytics__source_events__1782741465__dev"'
    )

    assert not db_api._is_virtual_layer_ddl(
        'CREATE MATERIALIZED VIEW "sqlmesh__analytics"."analytics__aggregate_view__1225616675__dev" AS '
        'SELECT * FROM "sqlmesh__analytics"."analytics__source_events__1782741465__dev"'
    )


def test_strip_table_qualifiers_preserves_current_timestamp_keyword() -> None:
    sql = (
        'CREATE MATERIALIZED VIEW "db"."view_model" AS '
        'SELECT CURRENT_TIMESTAMP AS ts FROM "db"."source"'
    )

    assert db_api._strip_table_qualifiers(sql) == (
        'CREATE MATERIALIZED VIEW "view_model" AS SELECT CURRENT_TIMESTAMP AS ts FROM "source"'
    )


def test_normalize_pipeline_ddl_canonicalizes_snapshot_names_to_logical_names() -> None:
    sql = (
        'CREATE MATERIALIZED VIEW "sqlmesh__analytics"."analytics__aggregate_view__1225616675__dev" AS '
        'SELECT "source_events"."entity_id" AS "entity_id" '
        'FROM "sqlmesh__analytics"."analytics__source_events__1782741465__dev" AS "source_events"'
    )

    assert db_api._normalize_pipeline_ddl(sql) == (
        'CREATE MATERIALIZED VIEW "aggregate_view" AS '
        'SELECT "source_events"."entity_id" AS "entity_id" '
        'FROM "source_events" AS "source_events"'
    )


def test_normalize_pipeline_ddl_strips_schema_from_logical_tables() -> None:
    sql = 'CREATE TABLE "analytics"."sample_seed" ("entity_id" VARCHAR, "description" VARCHAR)'

    assert db_api._normalize_pipeline_ddl(sql) == (
        'CREATE TABLE "sample_seed" ("entity_id" VARCHAR, "description" VARCHAR)'
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

    state_manager = t.cast(t.Any, FakeStateManager())
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


def test_cursor_ignores_virtual_layer_view_ddl() -> None:
    class FakeStateManager:
        def __init__(self) -> None:
            self.registered_sql: list[str] = []

        def register_ddl(self, sql: str) -> None:
            self.registered_sql.append(sql)

        def has_pending_changes(self) -> bool:
            return False

    state_manager = t.cast(t.Any, FakeStateManager())
    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=state_manager,
    )

    cursor.execute(
        'CREATE VIEW "analytics__dev"."source_events" AS '
        'SELECT * FROM "sqlmesh__analytics"."analytics__source_events__1782741465__dev"'
    )

    assert state_manager.registered_sql == []


def test_cursor_registers_logical_model_names_in_pipeline_ddl() -> None:
    class FakeStateManager:
        def __init__(self) -> None:
            self.registered_sql: list[str] = []

        def register_ddl(self, sql: str) -> None:
            self.registered_sql.append(sql)

        def has_pending_changes(self) -> bool:
            return False

    state_manager = t.cast(t.Any, FakeStateManager())
    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=state_manager,
    )

    cursor.execute(
        'CREATE MATERIALIZED VIEW "sqlmesh__analytics"."analytics__aggregated_observations__1956259901__dev" AS '
        'SELECT "source_events"."entity_id" AS "entity_id" '
        'FROM "sqlmesh__analytics"."analytics__source_events__1782741465__dev" AS "source_events"'
    )

    assert state_manager.registered_sql == [
        'CREATE MATERIALIZED VIEW "aggregated_observations" AS '
        'SELECT "source_events"."entity_id" AS "entity_id" '
        'FROM "source_events" AS "source_events"'
    ]


def test_hydrate_existing_program_skips_empty_parse_results(monkeypatch) -> None:
    manager = db_api.PipelineStateManager()

    monkeypatch.setattr(
        db_api,
        "parse",
        lambda sql, **kwargs: [None, parse_one("CREATE TABLE foo (id INT)", **kwargs)],
    )

    pipeline_module = types.ModuleType("feldera.pipeline")
    setattr(
        pipeline_module,
        "Pipeline",
        type(
            "Pipeline",
            (),
            {
                "get": staticmethod(
                    lambda pipeline_name, client: types.SimpleNamespace(
                        _inner=types.SimpleNamespace(program_code="ignored")
                    )
                )
            },
        ),
    )
    feldera_module = types.ModuleType("feldera")
    setattr(feldera_module, "pipeline", pipeline_module)

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
        'CREATE TABLE "seed_model" ("id" INTEGER);\n'
        "\n"
        'CREATE MATERIALIZED VIEW "__sqlmesh_query__seed_model" AS SELECT * FROM "seed_model";\n'
        "\n"
        'INSERT INTO "seed_model" (id) SELECT CAST("id" AS INTEGER) AS "id" FROM (VALUES (1)) AS "t"("id");'
    )


def test_evict_hydrated_objects_removes_stale_object_from_compile_error() -> None:
    manager = db_api.PipelineStateManager()
    manager._views = {
        "analytics__aggregate_view__781619724__dev": (
            'CREATE MATERIALIZED VIEW "analytics__aggregate_view__781619724__dev" AS '
            "SELECT CURRENT_TIMESTAMP AS ts"
        ),
        "analytics__aggregate_view__1225616675__dev": (
            'CREATE MATERIALIZED VIEW "analytics__aggregate_view__1225616675__dev" AS '
            "SELECT NOW() AS ts"
        ),
    }
    manager._hydrated_object_keys = {
        "analytics__aggregate_view__781619724__dev",
        "analytics__aggregate_view__1225616675__dev",
    }

    removed = manager._evict_hydrated_objects(
        'Compilation error in CREATE MATERIALIZED VIEW "analytics__aggregate_view__781619724__dev"'
    )

    assert removed is True
    assert "analytics__aggregate_view__781619724__dev" not in manager.pending_views()
    assert "analytics__aggregate_view__1225616675__dev" in manager.pending_views()


def test_format_compile_error_preserves_sql_compilation_details() -> None:
    manager = db_api.PipelineStateManager()

    class FakeClient:
        def get_pipeline(self, pipeline_name: str, field_selector: object) -> object:
            return types.SimpleNamespace(
                program_status="SqlError",
                program_error={
                    "sql_compilation": {
                        "messages": [
                            {
                                "error_type": "Compilation error",
                                "message": "Object 'analytics__source_events__1782741465__dev' not found",
                                "snippet": '1|CREATE MATERIALIZED VIEW "analytics__aggregate_view__1225616675__dev" AS SELECT ...',
                            }
                        ]
                    }
                },
            )

    error = manager._format_compile_error(
        FakeClient(),
        "test_pipeline",
        RuntimeError("The program failed to compile: SqlError"),
    )

    assert str(error) == (
        "Pipeline test_pipeline failed to compile:\n"
        "Compilation error\n"
        "Object 'analytics__source_events__1782741465__dev' not found\n"
        'Code snippet:\n1|CREATE MATERIALIZED VIEW "analytics__aggregate_view__1225616675__dev" AS SELECT ...'
    )


def test_format_compile_error_requests_all_pipeline_fields(monkeypatch) -> None:
    manager = db_api.PipelineStateManager()
    requested_field_selector: list[object] = []
    selector_all = object()

    enums_module = types.ModuleType("feldera.enums")
    setattr(enums_module, "PipelineFieldSelector", types.SimpleNamespace(ALL=selector_all))
    monkeypatch.setitem(__import__("sys").modules, "feldera.enums", enums_module)

    class FakeClient:
        def get_pipeline(self, pipeline_name: str, field_selector: object) -> object:
            requested_field_selector.append(field_selector)
            return types.SimpleNamespace(program_error={}, program_status="Unknown")

    manager._format_compile_error(FakeClient(), "test_pipeline", RuntimeError("boom"))

    assert requested_field_selector == [selector_all]


def test_deploy_imports_compilation_profile_from_feldera_enums(monkeypatch) -> None:
    manager = db_api.PipelineStateManager()

    class CompilationProfile(str):
        pass

    class Pipeline:
        @staticmethod
        def get(name: str, client: object) -> object:
            return types.SimpleNamespace(_inner=types.SimpleNamespace(program_code=""))

    class PipelineBuilder:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def create_or_replace(self, wait: bool = True) -> object:
            return types.SimpleNamespace(start=lambda: None)

    class RuntimeConfig:
        def __init__(self) -> None:
            self.workers = 0

        @classmethod
        def default(cls) -> "RuntimeConfig":
            return cls()

        def to_dict(self) -> dict[str, object]:
            return {}

    feldera_module = types.ModuleType("feldera")
    enums_module = types.ModuleType("feldera.enums")
    pipeline_module = types.ModuleType("feldera.pipeline")
    pipeline_builder_module = types.ModuleType("feldera.pipeline_builder")
    runtime_config_module = types.ModuleType("feldera.runtime_config")
    rest_module = types.ModuleType("feldera.rest")
    rest_errors_module = types.ModuleType("feldera.rest.errors")
    rest_pipeline_module = types.ModuleType("feldera.rest.pipeline")

    setattr(enums_module, "CompilationProfile", CompilationProfile)
    setattr(pipeline_module, "Pipeline", Pipeline)
    setattr(pipeline_builder_module, "PipelineBuilder", PipelineBuilder)
    setattr(runtime_config_module, "RuntimeConfig", RuntimeConfig)
    setattr(rest_errors_module, "FelderaAPIError", RuntimeError)
    setattr(rest_pipeline_module, "Pipeline", type("InnerPipeline", (), {}))
    setattr(rest_module, "errors", rest_errors_module)
    setattr(rest_module, "pipeline", rest_pipeline_module)
    setattr(feldera_module, "enums", enums_module)
    setattr(feldera_module, "pipeline", pipeline_module)
    setattr(feldera_module, "pipeline_builder", pipeline_builder_module)
    setattr(feldera_module, "runtime_config", runtime_config_module)
    setattr(feldera_module, "rest", rest_module)

    monkeypatch.setitem(__import__("sys").modules, "feldera", feldera_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.enums", enums_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.pipeline", pipeline_module)
    monkeypatch.setitem(
        __import__("sys").modules, "feldera.pipeline_builder", pipeline_builder_module
    )
    monkeypatch.setitem(__import__("sys").modules, "feldera.runtime_config", runtime_config_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.rest", rest_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.rest.errors", rest_errors_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.rest.pipeline", rest_pipeline_module)

    manager.deploy(object(), "test_pipeline")


def test_compile_program_does_not_wait_after_stop() -> None:
    manager = db_api.PipelineStateManager()
    wait_calls: list[tuple[object, int]] = []
    stop_calls: list[tuple[bool, t.Optional[float]]] = []
    dismiss_error_calls = 0

    class ExistingPipeline:
        def stop(self, force: bool = True, timeout_s: t.Optional[float] = None) -> None:
            stop_calls.append((force, timeout_s))

        def wait_for_status(self, status: object, timeout: int = 0) -> None:
            wait_calls.append((status, timeout))

        def dismiss_error(self) -> None:
            nonlocal dismiss_error_calls
            dismiss_error_calls += 1

    class Pipeline:
        @staticmethod
        def get(name: str, client: object) -> object:
            return ExistingPipeline()

        def __init__(self, client: object) -> None:
            self._inner = None

    class InnerPipeline:
        def __init__(self, **kwargs: object) -> None:
            pass

    class Profile:
        value = "dev"

    class RuntimeConfig:
        def to_dict(self) -> dict[str, object]:
            return {}

    client = types.SimpleNamespace(create_or_update_pipeline=lambda pipeline, wait=True: pipeline)

    manager._compile_program(
        client,
        "test_pipeline",
        "SELECT 1",
        Profile(),
        RuntimeConfig(),
        300,
        Pipeline,
        object,
        InnerPipeline,
        RuntimeError,
    )

    assert stop_calls == [(True, 300)]
    assert dismiss_error_calls == 1
    assert wait_calls == []


def test_deploy_does_not_wait_after_start(monkeypatch) -> None:
    manager = db_api.PipelineStateManager()
    manager._dirty = True
    start_calls: list[t.Optional[float]] = []
    wait_calls: list[tuple[object, int]] = []

    class Pipeline:
        def start(self, timeout_s: t.Optional[float] = None) -> None:
            start_calls.append(timeout_s)

        def wait_for_status(self, status: object, timeout: int = 0) -> None:
            wait_calls.append((status, timeout))

    class CompilationProfile(str):
        pass

    class RuntimeConfig:
        def __init__(self) -> None:
            self.workers = 0

        @classmethod
        def default(cls) -> "RuntimeConfig":
            return cls()

        def to_dict(self) -> dict[str, object]:
            return {}

    enums_module = types.ModuleType("feldera.enums")
    pipeline_module = types.ModuleType("feldera.pipeline")
    pipeline_builder_module = types.ModuleType("feldera.pipeline_builder")
    runtime_config_module = types.ModuleType("feldera.runtime_config")
    rest_errors_module = types.ModuleType("feldera.rest.errors")
    rest_pipeline_module = types.ModuleType("feldera.rest.pipeline")

    setattr(enums_module, "CompilationProfile", CompilationProfile)
    setattr(
        pipeline_module,
        "Pipeline",
        type("PipelineClass", (), {"get": staticmethod(lambda n, c: None)}),
    )
    setattr(pipeline_builder_module, "PipelineBuilder", object)
    setattr(runtime_config_module, "RuntimeConfig", RuntimeConfig)
    setattr(rest_errors_module, "FelderaAPIError", RuntimeError)
    setattr(rest_pipeline_module, "Pipeline", type("InnerPipeline", (), {}))

    monkeypatch.setitem(__import__("sys").modules, "feldera.enums", enums_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.pipeline", pipeline_module)
    monkeypatch.setitem(
        __import__("sys").modules, "feldera.pipeline_builder", pipeline_builder_module
    )
    monkeypatch.setitem(__import__("sys").modules, "feldera.runtime_config", runtime_config_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.rest.errors", rest_errors_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.rest.pipeline", rest_pipeline_module)

    monkeypatch.setattr(manager, "_hydrate_existing_program", lambda client, pipeline_name: None)
    monkeypatch.setattr(manager, "assemble_program", lambda: "CREATE TABLE x (id INT)")
    monkeypatch.setattr(manager, "_compile_program", lambda *args, **kwargs: Pipeline())

    manager.deploy(object(), "test_pipeline")

    assert start_calls == [300]
    assert wait_calls == []


def test_connection_close_logs_pending_compile_error(caplog: pytest.LogCaptureFixture) -> None:
    class FakeStateManager:
        def has_pending_changes(self) -> bool:
            return True

        def deploy(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError(
                "Pipeline test_pipeline failed to compile:\n"
                "Compilation error\n"
                "TIMESTAMP_TRUNC problem"
            )

    connection = db_api.FelderaConnection(
        client=object(),
        host="http://localhost:8080",
        pipeline_name="test_pipeline",
    )
    connection._state = t.cast(t.Any, FakeStateManager())

    with caplog.at_level("ERROR", logger="sqlmesh.engines.feldera.db_api"):
        with pytest.raises(RuntimeError, match="TIMESTAMP_TRUNC problem"):
            connection.close()

    assert (
        "Feldera pending DDL failed during connection close for pipeline test_pipeline"
        in caplog.text
    )
    assert "TIMESTAMP_TRUNC problem" in caplog.text


def test_state_manager_adds_query_mirrors_for_non_materialized_relations() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl('CREATE TABLE "full_model" ("id" INT)')
    manager.register_ddl('CREATE VIEW "view_model" AS SELECT "id" FROM "full_model"')
    manager.register_ddl(
        'CREATE MATERIALIZED VIEW "materialized_view_model" AS SELECT "id" FROM "full_model"'
    )

    program = manager.assemble_program()

    assert (
        'CREATE MATERIALIZED VIEW "__sqlmesh_query__full_model" AS SELECT * FROM "full_model";'
        in program
    )
    assert (
        'CREATE MATERIALIZED VIEW "__sqlmesh_query__view_model" AS SELECT * FROM "view_model";'
        in program
    )
    assert 'CREATE MATERIALIZED VIEW "__sqlmesh_query__materialized_view_model"' not in program


def test_state_manager_query_mirror_strips_schema_qualifiers() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl(
        'CREATE TABLE "analytics"."sample_seed" AS '
        'SELECT CAST("id" AS INTEGER) AS "id" FROM (VALUES (1)) AS "t"("id")'
    )

    program = manager.assemble_program()

    assert (
        'CREATE MATERIALIZED VIEW "__sqlmesh_query__sample_seed" AS SELECT * FROM "sample_seed";'
        in program
    )
    assert 'SELECT * FROM "analytics"."sample_seed"' not in program


def test_state_manager_skips_query_mirrors_for_sqlmesh_internal_objects() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl(
        'CREATE TABLE "sqlmesh__analytics"."analytics__source_snapshot__441175831__dev" '
        '("entity_id" VARCHAR, "metric_value" DOUBLE)'
    )

    program = manager.assemble_program()

    assert "__sqlmesh_query__analytics__source_snapshot__441175831__dev" not in program


def test_state_manager_canonicalizes_snapshot_tables_to_logical_names() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl(
        db_api._normalize_pipeline_ddl(
            'CREATE TABLE "sqlmesh__analytics"."analytics__records__849752499__dev" '
            '("entity_id" VARCHAR, "description" VARCHAR)'
        )
    )

    program = manager.assemble_program()

    assert 'CREATE TABLE "records" ("entity_id" VARCHAR, "description" VARCHAR);' in program
    assert (
        'CREATE MATERIALIZED VIEW "__sqlmesh_query__records" AS SELECT * FROM "records";' in program
    )
    assert "analytics__records__849752499__dev" not in program


def test_hydrate_existing_program_skips_query_mirrors(monkeypatch) -> None:
    manager = db_api.PipelineStateManager()

    pipeline_module = types.ModuleType("feldera.pipeline")
    setattr(
        pipeline_module,
        "Pipeline",
        type(
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
        ),
    )
    feldera_module = types.ModuleType("feldera")
    setattr(feldera_module, "pipeline", pipeline_module)

    monkeypatch.setitem(__import__("sys").modules, "feldera", feldera_module)
    monkeypatch.setitem(__import__("sys").modules, "feldera.pipeline", pipeline_module)

    manager._hydrate_existing_program(object(), "test_pipeline")

    assert manager.pending_tables() == {"full_model"}
    assert manager.pending_views() == {"view_model"}


def test_cursor_rewrites_queries_to_query_mirrors() -> None:
    captured_queries: list[str] = []

    class FakeStateManager:
        def has_pending_changes(self) -> bool:
            return False

        def queryable_relation_names(self) -> set[str]:
            return {"full_model", "view_model"}

        def current_pipeline(self) -> object:
            def query(sql: str) -> list[dict[str, int]]:
                captured_queries.append(sql)
                return [{"count": 1}]

            return types.SimpleNamespace(query=query)

    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=t.cast(t.Any, FakeStateManager()),
    )

    cursor.execute('SELECT COUNT(*) FROM "full_model"')

    assert (
        parse_one(captured_queries[0]).sql()
        == parse_one('SELECT COUNT(*) FROM "__sqlmesh_query__full_model"').sql()
    )
    assert cursor.fetchone() == (1,)


def test_cursor_rewrites_snapshot_queries_to_logical_query_mirrors() -> None:
    captured_queries: list[str] = []

    class FakeStateManager:
        def has_pending_changes(self) -> bool:
            return False

        def queryable_relation_names(self) -> set[str]:
            return {"source_events"}

        def current_pipeline(self) -> object:
            def query(sql: str) -> list[dict[str, int]]:
                captured_queries.append(sql)
                return [{"count": 1}]

            return types.SimpleNamespace(query=query)

    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=t.cast(t.Any, FakeStateManager()),
    )

    cursor.execute(
        'SELECT COUNT(*) FROM "sqlmesh__analytics"."analytics__source_events__1782741465__dev"'
    )

    assert (
        parse_one(captured_queries[0]).sql()
        == parse_one('SELECT COUNT(*) FROM "__sqlmesh_query__source_events"').sql()
    )
    assert cursor.fetchone() == (1,)


def test_insert_to_input_json_payload_rewrites_seed_values() -> None:
    payload = db_api._insert_to_input_json_payload(
        'INSERT INTO "seed_model" ("entity_id", "score", "event_ts") '
        'SELECT CAST("entity_id" AS TEXT) AS "entity_id", '
        'CAST("score" AS DOUBLE) AS "score", '
        'CAST("event_ts" AS TIMESTAMP) AS "event_ts" '
        "FROM (VALUES "
        "('123', '4.5', '2026-05-09 01:00:00'), "
        "('456', '7.0', NULL)"
        ') AS "t"("entity_id", "score", "event_ts")'
    )

    assert payload == (
        "seed_model",
        [
            {
                "entity_id": "123",
                "score": 4.5,
                "event_ts": "2026-05-09 01:00:00",
            },
            {
                "entity_id": "456",
                "score": 7.0,
                "event_ts": None,
            },
        ],
    )


def test_insert_to_input_json_payload_canonicalizes_snapshot_target() -> None:
    payload = db_api._insert_to_input_json_payload(
        'INSERT INTO "sqlmesh__analytics"."analytics__sample_seed__2883946936__dev" ("entity_id") '
        'SELECT CAST("entity_id" AS TEXT) AS "entity_id" '
        'FROM (VALUES (\'123\')) AS "t"("entity_id")'
    )

    assert payload == (
        "sample_seed",
        [
            {
                "entity_id": "123",
            }
        ],
    )


def test_cursor_uses_input_json_for_seed_inserts() -> None:
    captured_rows = []
    executed_sql = []

    class FakePipeline:
        def input_json(self, table_name: str, data: object, **kwargs: object) -> None:
            captured_rows.append((table_name, data, kwargs))

        def execute(self, sql: str) -> None:
            executed_sql.append(sql)

    class FakeStateManager:
        def has_pending_changes(self) -> bool:
            return False

        def current_pipeline(self) -> object:
            return FakePipeline()

        def queryable_relation_names(self) -> set[str]:
            return set()

    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=t.cast(t.Any, FakeStateManager()),
    )

    cursor.execute(
        'INSERT INTO "seed_model" ("a", "b") '
        'SELECT CAST("a" AS INT) AS "a", CAST("b" AS INT) AS "b" '
        'FROM (VALUES (1, 4), (2, 5)) AS "t"("a", "b")'
    )

    assert executed_sql == []
    assert captured_rows == [
        (
            "seed_model",
            [
                {"a": 1, "b": 4},
                {"a": 2, "b": 5},
            ],
            {},
        )
    ]


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
        state_manager=t.cast(t.Any, FakeStateManager()),
    )

    with pytest.raises(RuntimeError, match="Execution error: test failure"):
        cursor.execute("SELECT COUNT(*) FROM full_model")
