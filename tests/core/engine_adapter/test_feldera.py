# type: ignore

import sys
import types
import typing as t

import pytest
from sqlglot import exp

from sqlmesh.core.engine_adapter import FelderaEngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObjectType
from sqlmesh.utils.errors import SQLMeshError

pytestmark = [pytest.mark.engine]


@pytest.fixture
def adapter(make_mocked_engine_adapter: t.Callable) -> FelderaEngineAdapter:
    return make_mocked_engine_adapter(FelderaEngineAdapter, patch_get_data_objects=False)


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


def test_columns_uses_pipeline_metadata(adapter: FelderaEngineAdapter, monkeypatch: pytest.MonkeyPatch):
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
            views=lambda: [types.SimpleNamespace(name="sink")],
        )

    pipeline_cls = type("Pipeline", (), {"get": staticmethod(get_pipeline)})
    pipeline_module = types.ModuleType("feldera.pipeline")
    pipeline_module.Pipeline = pipeline_cls

    feldera_module = types.ModuleType("feldera")
    feldera_module.pipeline = pipeline_module

    monkeypatch.setitem(sys.modules, "feldera", feldera_module)
    monkeypatch.setitem(sys.modules, "feldera.pipeline", pipeline_module)

    data_objects = adapter._get_data_objects("catalog.requested_pipeline")

    assert requested_pipeline_names == ["requested_pipeline"]
    assert [(obj.schema_name, obj.name, obj.type) for obj in data_objects] == [
        ("requested_pipeline", "source", DataObjectType.TABLE),
        ("requested_pipeline", "sink", DataObjectType.MATERIALIZED_VIEW),
    ]
    assert adapter.dialect == "felderadialect"