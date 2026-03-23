import os
import time
import typing as t
from pathlib import Path

from pytest_mock.plugin import MockerFixture
from sqlglot import parse_one

from sqlmesh.core import dialect as d
from sqlmesh.core.model import SqlModel, load_sql_based_model
from sqlmesh.core.model.cache import OptimizedQueryCache
from sqlmesh.utils.cache import FileCache
from sqlmesh.utils.pydantic import PydanticModel


class _TestEntry(PydanticModel):
    value: str


def test_file_cache(tmp_path: Path, mocker: MockerFixture):
    cache: FileCache[_TestEntry] = FileCache(tmp_path)

    test_entry_a = _TestEntry(value="value_a")
    test_entry_b = _TestEntry(value="value_b")

    loader = mocker.Mock(return_value=test_entry_a)

    assert cache.get("test_name", "test_entry_a") is None

    assert cache.get_or_load("test_name", "test_entry_a", loader=loader) == test_entry_a
    assert cache.get_or_load("test_name", "test_entry_a", loader=loader) == test_entry_a
    assert cache.get("test_name", "test_entry_a") == test_entry_a

    cache.put("test_name", "test_entry_b", value=test_entry_b)
    assert cache.get("test_name", "test_entry_b") == test_entry_b
    assert cache.get_or_load("test_name", "test_entry_b", loader=loader) == test_entry_b
    assert cache.get("test_name", "test_entry_a") == test_entry_a

    assert cache.get("different_name", "test_entry_b") is None

    loader.assert_called_once()

    assert "___test_model_" in cache._cache_entry_path('"test_model"').name
    assert "客户数据" in cache._cache_entry_path("客户数据").name


def test_optimized_query_cache(tmp_path: Path, mocker: MockerFixture):
    model = SqlModel(
        name="test_model",
        query=parse_one("SELECT a FROM tbl"),
        mapping_schema={"tbl": {"a": "int"}},
    )

    cache = OptimizedQueryCache(tmp_path)

    assert not cache.with_optimized_query(model)

    model._query_renderer._cache = []
    model._query_renderer._optimized_cache = None

    assert cache.with_optimized_query(model)

    assert not model._query_renderer._cache
    assert model._query_renderer._optimized_cache is not None


def test_optimized_query_cache_missing_rendered_query(tmp_path: Path, mocker: MockerFixture):
    model = SqlModel(
        name="test_model",
        query=parse_one("SELECT a FROM tbl"),
        mapping_schema={"tbl": {"a": "int"}},
    )
    render_mock = mocker.patch.object(model._query_renderer, "render")
    render_mock.return_value = None

    cache = OptimizedQueryCache(tmp_path)

    assert not cache.with_optimized_query(model)

    model._query_renderer._cache = []
    model._query_renderer._optimized_cache = None

    assert cache.with_optimized_query(model)

    assert model._query_renderer._cache == [None]
    assert model._query_renderer._optimized_cache is None


def test_optimized_query_cache_macro_def_change(tmp_path: Path, mocker: MockerFixture):
    expressions = d.parse(
        """
        MODEL (name db.table);

        @DEF(filter_, a = 1);

        SELECT a FROM (SELECT 1 AS a) WHERE @filter_;
        """
    )
    model = t.cast(SqlModel, load_sql_based_model(expressions))

    cache = OptimizedQueryCache(tmp_path)

    assert not cache.with_optimized_query(model)

    model._query_renderer._cache = []
    model._query_renderer._optimized_cache = None

    assert cache.with_optimized_query(model)
    assert (
        model.render_query_or_raise().sql()
        == 'SELECT "_0"."a" AS "a" FROM (SELECT 1 AS "a") AS "_0" WHERE "_0"."a" = 1'
    )

    # Change the filter_ definition
    new_expressions = d.parse(
        """
        MODEL (name db.table);

        @DEF(filter_, a = 2);

        SELECT a FROM (SELECT 1 AS a) WHERE @filter_;
        """
    )
    new_model = t.cast(SqlModel, load_sql_based_model(new_expressions))

    assert not cache.with_optimized_query(new_model)

    new_model._query_renderer._cache = []
    new_model._query_renderer._optimized_cache = None

    assert cache.with_optimized_query(new_model)
    assert (
        new_model.render_query_or_raise().sql()
        == 'SELECT "_0"."a" AS "a" FROM (SELECT 1 AS "a") AS "_0" WHERE "_0"."a" = 2'
    )


def test_file_cache_init_handles_stale_file(tmp_path: Path, mocker: MockerFixture) -> None:
    cache: FileCache[_TestEntry] = FileCache(tmp_path)

    stale_file = tmp_path / f"{cache._cache_version}__fake_deleted_model_9999999999"
    stale_file.touch()

    original_stat = Path.stat

    def flaky_stat(self, **kwargs):
        if self.name == stale_file.name:
            raise FileNotFoundError(f"Simulated stale file: {self}")
        return original_stat(self, **kwargs)

    mocker.patch.object(Path, "stat", flaky_stat)

    FileCache(tmp_path)
    