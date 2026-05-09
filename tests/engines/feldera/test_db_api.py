import types

import pytest
from sqlglot import parse_one

from sqlmesh.engines.feldera import db_api


def test_classify_treats_comment_prefixed_create_schema_as_pipeline_ddl() -> None:
    assert (
        db_api._classify("/* sqlmesh */ CREATE SCHEMA foo")
        == db_api.SqlIntent.PIPELINE_DDL
    )


def test_is_virtual_layer_ddl_identifies_environment_alias_view() -> None:
    assert db_api._is_virtual_layer_ddl(
        'CREATE VIEW "polymarket__dev"."live_trades" AS '
        'SELECT * FROM "sqlmesh__polymarket"."polymarket__live_trades__1782741465__dev"'
    )

    assert not db_api._is_virtual_layer_ddl(
        'CREATE MATERIALIZED VIEW "sqlmesh__polymarket"."polymarket__rolling_vwap__1225616675__dev" AS '
        'SELECT * FROM "sqlmesh__polymarket"."polymarket__live_trades__1782741465__dev"'
    )


def test_strip_table_qualifiers_preserves_current_timestamp_keyword() -> None:
    sql = (
        'CREATE MATERIALIZED VIEW "db"."view_model" AS '
        'SELECT CURRENT_TIMESTAMP AS ts FROM "db"."source"'
    )

    assert db_api._strip_table_qualifiers(sql) == (
        'CREATE MATERIALIZED VIEW "view_model" AS '
        'SELECT CURRENT_TIMESTAMP AS ts FROM "source"'
    )


def test_normalize_pipeline_ddl_strips_only_sqlmesh_internal_qualifiers() -> None:
    sql = (
        'CREATE MATERIALIZED VIEW "sqlmesh__polymarket"."polymarket__rolling_vwap__1225616675__dev" AS '
        'SELECT "live_trades"."market_id" AS "market_id" '
        'FROM "polymarket"."live_trades" AS "live_trades"'
    )

    assert db_api._normalize_pipeline_ddl(sql) == (
        'CREATE MATERIALIZED VIEW "polymarket__rolling_vwap__1225616675__dev" AS '
        'SELECT "live_trades"."market_id" AS "market_id" '
        'FROM "polymarket"."live_trades" AS "live_trades"'
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


def test_cursor_ignores_virtual_layer_view_ddl() -> None:
    class FakeStateManager:
        def __init__(self) -> None:
            self.registered_sql = []

        def register_ddl(self, sql: str) -> None:
            self.registered_sql.append(sql)

        def has_pending_changes(self) -> bool:
            return False

    state_manager = FakeStateManager()
    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=state_manager,
    )

    cursor.execute(
        'CREATE VIEW "polymarket__dev"."live_trades" AS '
        'SELECT * FROM "sqlmesh__polymarket"."polymarket__live_trades__1782741465__dev"'
    )

    assert state_manager.registered_sql == []


def test_cursor_preserves_schema_qualified_references_in_pipeline_ddl() -> None:
    class FakeStateManager:
        def __init__(self) -> None:
            self.registered_sql = []

        def register_ddl(self, sql: str) -> None:
            self.registered_sql.append(sql)

        def has_pending_changes(self) -> bool:
            return False

    state_manager = FakeStateManager()
    cursor = db_api.FelderaCursor(
        client=object(),
        pipeline_name="test_pipeline",
        state_manager=state_manager,
    )

    cursor.execute(
        'CREATE MATERIALIZED VIEW "sqlmesh__polymarket"."polymarket__trade_price_observations__1956259901__dev" AS '
        'SELECT "live_trades"."market_id" AS "market_id" '
        'FROM "polymarket"."live_trades" AS "live_trades"'
    )

    assert state_manager.registered_sql == [
        'CREATE MATERIALIZED VIEW "polymarket__trade_price_observations__1956259901__dev" AS '
        'SELECT "live_trades"."market_id" AS "market_id" '
        'FROM "polymarket"."live_trades" AS "live_trades"'
    ]


def test_hydrate_existing_program_skips_empty_parse_results(monkeypatch) -> None:
    manager = db_api.PipelineStateManager()

    monkeypatch.setattr(
        db_api,
        "parse",
        lambda sql, **kwargs: [None, parse_one("CREATE TABLE foo (id INT)", **kwargs)],
    )

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
        'CREATE TABLE "seed_model" ("id" INTEGER);\n'
        '\n'
        'CREATE MATERIALIZED VIEW "__sqlmesh_query__seed_model" AS SELECT * FROM "seed_model";\n'
        '\n'
        'INSERT INTO "seed_model" (id) SELECT CAST("id" AS INTEGER) AS "id" FROM (VALUES (1)) AS "t"("id");'
    )


def test_evict_hydrated_objects_removes_stale_object_from_compile_error() -> None:
    manager = db_api.PipelineStateManager()
    manager._views = {
        'polymarket__rolling_vwap__781619724__dev': (
            'CREATE MATERIALIZED VIEW "polymarket__rolling_vwap__781619724__dev" AS '
            'SELECT CURRENT_TIMESTAMP AS ts'
        ),
        'polymarket__rolling_vwap__1225616675__dev': (
            'CREATE MATERIALIZED VIEW "polymarket__rolling_vwap__1225616675__dev" AS '
            'SELECT NOW() AS ts'
        ),
    }
    manager._hydrated_object_keys = {
        'polymarket__rolling_vwap__781619724__dev',
        'polymarket__rolling_vwap__1225616675__dev',
    }

    removed = manager._evict_hydrated_objects(
        'Compilation error in CREATE MATERIALIZED VIEW "polymarket__rolling_vwap__781619724__dev"'
    )

    assert removed is True
    assert 'polymarket__rolling_vwap__781619724__dev' not in manager.pending_views()
    assert 'polymarket__rolling_vwap__1225616675__dev' in manager.pending_views()


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
                                "message": "Object 'polymarket__live_trades__1782741465__dev' not found",
                                "snippet": '1|CREATE MATERIALIZED VIEW "polymarket__rolling_vwap__1225616675__dev" AS SELECT ...',
                            }
                        ]
                    }
                },
            )

    error = manager._format_compile_error(
        FakeClient(),
        "polymarket",
        RuntimeError("The program failed to compile: SqlError"),
    )

    assert str(error) == (
        "Pipeline polymarket failed to compile:\n"
        "Compilation error\n"
        "Object 'polymarket__live_trades__1782741465__dev' not found\n"
        'Code snippet:\n1|CREATE MATERIALIZED VIEW "polymarket__rolling_vwap__1225616675__dev" AS SELECT ...'
    )


def test_connection_close_logs_pending_compile_error(caplog: pytest.LogCaptureFixture) -> None:
    class FakeStateManager:
        def has_pending_changes(self) -> bool:
            return True

        def deploy(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError(
                "Pipeline polymarket failed to compile:\n"
                "Compilation error\n"
                "TIMESTAMP_TRUNC problem"
            )

    connection = db_api.FelderaConnection(
        client=object(),
        host="http://localhost:8080",
        pipeline_name="polymarket",
    )
    connection._state = FakeStateManager()

    with caplog.at_level("ERROR", logger="sqlmesh.engines.feldera.db_api"):
        with pytest.raises(RuntimeError, match="TIMESTAMP_TRUNC problem"):
            connection.close()

    assert "Feldera pending DDL failed during connection close for pipeline polymarket" in caplog.text
    assert "TIMESTAMP_TRUNC problem" in caplog.text


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


def test_state_manager_query_mirror_strips_schema_qualifiers() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl(
        'CREATE TABLE "polymarket"."markets_sample_seed" AS '
        'SELECT CAST("id" AS INTEGER) AS "id" FROM (VALUES (1)) AS "t"("id")'
    )

    program = manager.assemble_program()

    assert 'CREATE MATERIALIZED VIEW "__sqlmesh_query__markets_sample_seed" AS SELECT * FROM "markets_sample_seed";' in program
    assert 'SELECT * FROM "polymarket"."markets_sample_seed"' not in program


def test_state_manager_skips_query_mirrors_for_sqlmesh_internal_objects() -> None:
    manager = db_api.PipelineStateManager()

    manager.register_ddl(
        'CREATE TABLE "sqlmesh__polymarket"."polymarket__l1_orderbook__441175831__dev" '
        '("market_id" VARCHAR, "best_bid" DOUBLE)'
    )

    program = manager.assemble_program()

    assert '__sqlmesh_query__polymarket__l1_orderbook__441175831__dev' not in program


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


def test_insert_to_input_json_payload_rewrites_seed_values() -> None:
    payload = db_api._insert_to_input_json_payload(
        'INSERT INTO "seed_model" ("market_id", "volume_24h", "end_date") '
        'SELECT CAST("market_id" AS TEXT) AS "market_id", '
        'CAST("volume_24h" AS DOUBLE) AS "volume_24h", '
        'CAST("end_date" AS TIMESTAMP) AS "end_date" '
        'FROM (VALUES '
        "('123', '4.5', '2026-05-09 01:00:00'), "
        "('456', '7.0', NULL)"
        ') AS "t"("market_id", "volume_24h", "end_date")'
    )

    assert payload == (
        "seed_model",
        [
            {
                "market_id": "123",
                "volume_24h": 4.5,
                "end_date": "2026-05-09 01:00:00",
            },
            {
                "market_id": "456",
                "volume_24h": 7.0,
                "end_date": None,
            },
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
        state_manager=FakeStateManager(),
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
        state_manager=FakeStateManager(),
    )

    with pytest.raises(RuntimeError, match="Execution error: test failure"):
        cursor.execute("SELECT COUNT(*) FROM full_model")