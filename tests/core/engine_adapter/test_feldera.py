# type: ignore

import sys
import types
import typing as t

import pytest
from sqlglot import Dialect
from sqlglot import parse_one
from sqlglot import exp

from sqlmesh.core.engine_adapter import FelderaEngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObject, DataObjectType
from sqlmesh.engines.feldera.dialect import register_feldera_dialect
from sqlmesh.utils.errors import SQLMeshError, UnsupportedCatalogOperationError
from tests.core.engine_adapter import to_sql_calls

pytestmark = [pytest.mark.engine, pytest.mark.feldera]


@pytest.fixture
def adapter(make_mocked_engine_adapter: t.Callable) -> FelderaEngineAdapter:
    adapter = make_mocked_engine_adapter(FelderaEngineAdapter, patch_get_data_objects=False)
    connection = adapter._connection_pool.get()
    connection._state.pending_drops.return_value = set()
    connection._state.pending_tables.return_value = set()
    connection._state.pending_views.return_value = set()
    connection._state.is_materialized_view.side_effect = lambda _: False
    return adapter


def _install_feldera_pipeline(monkeypatch: pytest.MonkeyPatch, pipeline: t.Any) -> None:
    pipeline_cls = type(
        "Pipeline",
        (),
        {"get": staticmethod(lambda pipeline_name, client: pipeline)},
    )
    pipeline_module = types.ModuleType("feldera.pipeline")
    pipeline_module.Pipeline = pipeline_cls

    feldera_module = types.ModuleType("feldera")
    feldera_module.pipeline = pipeline_module

    monkeypatch.setitem(sys.modules, "feldera", feldera_module)
    monkeypatch.setitem(sys.modules, "feldera.pipeline", pipeline_module)


def test_columns_uses_pipeline_metadata(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    connection = adapter._connection_pool.get()
    connection._client = object()
    connection._pipeline_name = "configured_pipeline"

    pipeline = types.SimpleNamespace(
        tables=lambda: [
            types.SimpleNamespace(
                name="orders",
                fields=[
                    {"name": "id", "columntype": {"type": "INTEGER"}},
                    {"name": "ratio", "columntype": {"type": "REAL"}},
                    {"name": "name", "columntype": "VARCHAR(12)"},
                ],
            )
        ],
        views=lambda: [],
    )
    _install_feldera_pipeline(monkeypatch, pipeline)

    assert adapter.columns("orders") == {
        "id": exp.DataType.build("INT"),
        "ratio": exp.DataType.build("FLOAT"),
        "name": exp.DataType.build("VARCHAR"),
    }


def test_columns_raises_sqlmesh_error_for_missing_object(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    connection = adapter._connection_pool.get()
    connection._client = object()
    connection._pipeline_name = "configured_pipeline"

    pipeline = types.SimpleNamespace(tables=lambda: [], views=lambda: [])
    _install_feldera_pipeline(monkeypatch, pipeline)

    with pytest.raises(SQLMeshError, match="missing"):
        adapter.columns("missing")


def test_get_data_objects_uses_requested_pipeline_name(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    connection = adapter._connection_pool.get()
    connection._client = object()
    connection._pipeline_name = "configured_pipeline"

    requested_pipeline_names: t.List[str] = []

    def get_pipeline(pipeline_name: str, client: t.Any) -> t.Any:
        requested_pipeline_names.append(pipeline_name)
        return types.SimpleNamespace(
            tables=lambda: [types.SimpleNamespace(name="source")],
            views=lambda: [
                types.SimpleNamespace(name="sink"),
                types.SimpleNamespace(name="__sqlmesh_query__source"),
            ],
        )

    pipeline_cls = type("Pipeline", (), {"get": staticmethod(get_pipeline)})
    pipeline_module = types.ModuleType("feldera.pipeline")
    pipeline_module.Pipeline = pipeline_cls

    feldera_module = types.ModuleType("feldera")
    feldera_module.pipeline = pipeline_module

    monkeypatch.setitem(sys.modules, "feldera", feldera_module)
    monkeypatch.setitem(sys.modules, "feldera.pipeline", pipeline_module)

    data_objects = adapter._get_data_objects("requested_pipeline")

    assert requested_pipeline_names == ["requested_pipeline"]
    assert [(obj.schema_name, obj.name, obj.type) for obj in data_objects] == [
        ("requested_pipeline", "source", DataObjectType.TABLE),
        ("requested_pipeline", "sink", DataObjectType.VIEW),
    ]


def test_adapter_dialect_is_feldera(adapter: FelderaEngineAdapter) -> None:
    assert adapter.dialect == "feldera"


def test_feldera_dialect_is_registered() -> None:
    assert parse_one("SELECT 1", dialect="feldera").sql(dialect="feldera") == "SELECT 1"


def test_feldera_dialect_preserves_current_timestamp_keyword() -> None:
    assert (
        parse_one("SELECT CURRENT_TIMESTAMP AS ts", dialect="feldera").sql(dialect="feldera")
        == "SELECT CURRENT_TIMESTAMP AS ts"
    )


def test_register_feldera_dialect_registers_custom_type_mapping() -> None:
    original = Dialect.classes.pop("feldera", None)

    try:
        register_feldera_dialect()
        assert exp.DataType.build("FLOAT").sql(dialect="feldera") == "REAL"
    finally:
        Dialect.classes.pop("feldera", None)
        if original is not None:
            Dialect.classes["feldera"] = original
        else:
            register_feldera_dialect()


def test_get_data_objects_marks_materialized_views_from_state(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    connection = adapter._connection_pool.get()
    connection._client = object()
    connection._state.is_materialized_view.side_effect = lambda name: name == "sink"

    pipeline = types.SimpleNamespace(
        tables=lambda: [],
        views=lambda: [types.SimpleNamespace(name="sink")],
    )
    _install_feldera_pipeline(monkeypatch, pipeline)

    data_objects = adapter._get_data_objects("requested_pipeline")

    assert [(obj.schema_name, obj.name, obj.type) for obj in data_objects] == [
        ("requested_pipeline", "sink", DataObjectType.MATERIALIZED_VIEW),
    ]


def test_replace_query_creates_table(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(adapter, "_get_data_objects", lambda schema_name, object_names=None: [])
    adapter.replace_query(
        "db.full_model",
        parse_one("SELECT a FROM tbl"),
        {"a": exp.DataType.build("INT")},
    )

    assert to_sql_calls(adapter) == [
        'CREATE TABLE IF NOT EXISTS "db"."full_model" ("a" INTEGER)',
        'INSERT INTO "db"."full_model" ("a") SELECT "a" FROM "tbl"',
    ]


def test_replace_query_recreates_existing_table(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        adapter,
        "get_data_object",
        lambda table: DataObject(schema="db", name="full_model", type=DataObjectType.TABLE),
    )

    adapter.replace_query(
        "db.full_model",
        parse_one("SELECT a FROM tbl"),
        {"a": exp.DataType.build("INT")},
    )

    assert to_sql_calls(adapter) == [
        'DROP TABLE IF EXISTS "db"."full_model"',
        'CREATE TABLE IF NOT EXISTS "db"."full_model" ("a" INTEGER)',
        'INSERT INTO "db"."full_model" ("a") SELECT "a" FROM "tbl"',
    ]


def test_insert_overwrite_by_time_partition_uses_delete_insert(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(adapter, "_get_data_objects", lambda schema_name, object_names=None: [])

    adapter.insert_overwrite_by_time_partition(
        "db.incremental_model",
        parse_one("SELECT id, ds FROM source"),
        start="2024-01-01",
        end="2024-01-02",
        time_column="ds",
        time_formatter=lambda x, _: exp.Literal.string(str(x)[:10]),
        target_columns_to_types={
            "id": exp.DataType.build("INT"),
            "ds": exp.DataType.build("DATE"),
        },
    )

    assert to_sql_calls(adapter) == [
        'DELETE FROM "db"."incremental_model" WHERE "ds" BETWEEN \'2024-01-01\' AND \'2024-01-02\'',
        'INSERT INTO "db"."incremental_model" ("id", "ds") SELECT "id", "ds" FROM (SELECT "id", "ds" FROM "source") AS "_subquery" WHERE "ds" BETWEEN \'2024-01-01\' AND \'2024-01-02\'',
    ]


def test_insert_overwrite_without_condition_drops_and_recreates_table(
    adapter: FelderaEngineAdapter,
) -> None:
    recorded_calls: list[tuple[str, tuple[t.Any, ...], dict[str, t.Any]]] = []
    source_queries = [t.cast(t.Any, object())]
    target_columns_to_types = {"a": exp.DataType.build("INT")}

    def record_drop_table(*args: t.Any, **kwargs: t.Any) -> None:
        recorded_calls.append(("drop_table", args, kwargs))

    def record_create_table(*args: t.Any, **kwargs: t.Any) -> None:
        recorded_calls.append(("create_table", args, kwargs))

    adapter.drop_table = record_drop_table  # type: ignore[method-assign]
    adapter._create_table_from_source_queries = record_create_table  # type: ignore[method-assign]

    adapter._insert_overwrite_by_condition(
        "db.full_model",
        source_queries,
        target_columns_to_types=target_columns_to_types,
        where=None,
    )

    assert recorded_calls == [
        ("drop_table", ("db.full_model",), {}),
        (
            "create_table",
            ("db.full_model", source_queries),
            {
                "target_columns_to_types": target_columns_to_types,
                "exists": True,
                "replace": False,
            },
        ),
    ]


def test_create_table_from_source_queries_requires_known_column_types(
    adapter: FelderaEngineAdapter,
) -> None:
    with pytest.raises(SQLMeshError, match="requires known column types"):
        adapter._create_table_from_source_queries("db.full_model", [], target_columns_to_types=None)


def test_create_schema_is_no_op(adapter: FelderaEngineAdapter) -> None:
    assert adapter.create_schema("db") is None


def test_create_view_creates_view(adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(adapter, "_get_data_objects", lambda schema_name, object_names=None: [])
    adapter.create_view(
        "db.view_model",
        parse_one("SELECT a FROM tbl"),
        replace=False,
        materialized=False,
    )

    assert to_sql_calls(adapter) == [
        'CREATE VIEW "db"."view_model" AS SELECT "a" FROM "tbl"',
    ]


def test_create_view_rejects_catalog_qualified_names(adapter: FelderaEngineAdapter) -> None:
    with pytest.raises(UnsupportedCatalogOperationError, match="does not support catalogs"):
        adapter.create_view(
            "catalog.db.view_model",
            parse_one("SELECT a FROM tbl"),
        )


def test_replace_view_drops_then_creates_view(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        adapter,
        "get_data_object",
        lambda table: DataObject(schema="db", name="view_model", type=DataObjectType.VIEW),
    )

    adapter.create_view(
        "db.view_model",
        parse_one("SELECT a FROM tbl"),
        replace=True,
        materialized=False,
    )

    assert to_sql_calls(adapter) == [
        'DROP VIEW IF EXISTS "db"."view_model"',
        'CREATE VIEW "db"."view_model" AS SELECT "a" FROM "tbl"',
    ]


def test_create_materialized_view_creates_materialized_view(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(adapter, "_get_data_objects", lambda schema_name, object_names=None: [])
    adapter.create_view(
        "db.materialized_view_model",
        parse_one("SELECT a FROM tbl"),
        replace=False,
        materialized=True,
    )

    assert to_sql_calls(adapter) == [
        'CREATE MATERIALIZED VIEW "db"."materialized_view_model" AS SELECT "a" FROM "tbl"',
    ]


def test_replace_materialized_view_drops_then_creates_materialized_view(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        adapter,
        "get_data_object",
        lambda table: DataObject(
            schema="db", name="materialized_view_model", type=DataObjectType.MATERIALIZED_VIEW
        ),
    )

    adapter.create_view(
        "db.materialized_view_model",
        parse_one("SELECT a FROM tbl"),
        replace=True,
        materialized=True,
    )

    assert to_sql_calls(adapter) == [
        'DROP MATERIALIZED VIEW IF EXISTS "db"."materialized_view_model"',
        'CREATE MATERIALIZED VIEW "db"."materialized_view_model" AS SELECT "a" FROM "tbl"',
    ]


def test_drop_table_drops_view(adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        adapter,
        "get_data_object",
        lambda table: DataObject(schema="db", name="view_model", type=DataObjectType.VIEW),
    )

    adapter.drop_table("db.view_model")

    assert to_sql_calls(adapter) == ['DROP VIEW IF EXISTS "db"."view_model"']


def test_drop_table_drops_materialized_view(
    adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        adapter,
        "get_data_object",
        lambda table: DataObject(
            schema="db", name="materialized_view_model", type=DataObjectType.MATERIALIZED_VIEW
        ),
    )

    adapter.drop_table("db.materialized_view_model")

    assert to_sql_calls(adapter) == [
        'DROP MATERIALIZED VIEW IF EXISTS "db"."materialized_view_model"'
    ]


def test_drop_table_drops_table(adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        adapter,
        "get_data_object",
        lambda table: DataObject(schema="db", name="full_model", type=DataObjectType.TABLE),
    )

    adapter.drop_table("db.full_model")

    assert to_sql_calls(adapter) == ['DROP TABLE IF EXISTS "db"."full_model"']
