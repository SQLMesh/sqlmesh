# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import typing as t
from unittest.mock import call

import pytest
from sqlglot import exp

from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.engine_adapter.shared import CatalogSupport, set_catalog

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName

pytestmark = pytest.mark.engine


@set_catalog()
class CatalogAdapter(EngineAdapter):
    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.REQUIRES_SET_CATALOG

    def table_operation(self, table_name: TableName, callback: t.Callable) -> t.Any:
        return callback(table_name)

    def schema_operation(self, schema_name: SchemaName, callback: t.Callable) -> t.Any:
        return callback(schema_name)


@pytest.mark.parametrize("is_schema", [True, False])
@pytest.mark.parametrize("as_keyword", [True, False])
@pytest.mark.parametrize("fails", [True, False])
def test_set_catalog_operation(make_mocked_engine_adapter, mocker, is_schema, as_keyword, fails):
    adapter = make_mocked_engine_adapter(CatalogAdapter)
    mocker.patch.object(adapter, "get_current_catalog", return_value="original")
    switch = mocker.patch.object(adapter, "set_current_catalog")
    target = (
        exp.table_("", db="schema", catalog="temporary")
        if is_schema
        else exp.to_table("temporary.schema.table")
    )
    original_target = target.copy()
    result = object()
    error = RuntimeError("operation failed")
    callback = mocker.Mock(side_effect=error if fails else None, return_value=result)
    operation = adapter.schema_operation if is_schema else adapter.table_operation
    arguments = {"schema_name" if is_schema else "table_name": target, "callback": callback}

    def run():
        return operation(**arguments) if as_keyword else operation(target, callback)

    if fails:
        with pytest.raises(RuntimeError) as exc_info:
            run()
        assert exc_info.value is error
    else:
        assert run() is result

    assert target == original_target
    stripped_target = original_target.copy()
    stripped_target.set("catalog", None)
    callback.assert_called_once_with(stripped_target)
    assert switch.call_args_list == [call("temporary"), call("original")]


@pytest.mark.parametrize("target", ["original.schema.table", "schema.table"])
def test_set_catalog_skips_unneeded_switch(make_mocked_engine_adapter, mocker, target):
    adapter = make_mocked_engine_adapter(CatalogAdapter)
    get_catalog = mocker.patch.object(adapter, "get_current_catalog", return_value="original")
    switch = mocker.patch.object(adapter, "set_current_catalog")
    error = RuntimeError("operation failed")
    callback = mocker.Mock(side_effect=error)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.table_operation(target, callback)

    assert exc_info.value is error
    switch.assert_not_called()
    assert get_catalog.call_count == (1 if target.startswith("original.") else 0)


@pytest.mark.parametrize("fails", [True, False])
def test_set_catalog_nested_operations(make_mocked_engine_adapter, mocker, fails):
    adapter = make_mocked_engine_adapter(CatalogAdapter)
    current_catalog = "original"

    def set_current_catalog(catalog):
        nonlocal current_catalog
        current_catalog = catalog

    mocker.patch.object(adapter, "get_current_catalog", side_effect=lambda: current_catalog)
    switch = mocker.patch.object(adapter, "set_current_catalog", side_effect=set_current_catalog)
    result = object()
    error = RuntimeError("inner operation failed")

    def inner(table):
        assert current_catalog == "inner"
        if fails:
            raise error
        return result

    def outer(table):
        assert current_catalog == "outer"
        try:
            return adapter.table_operation("inner.schema.table", inner)
        finally:
            assert current_catalog == "outer"

    if fails:
        with pytest.raises(RuntimeError) as exc_info:
            adapter.table_operation("outer.schema.table", outer)
        assert exc_info.value is error
    else:
        assert adapter.table_operation("outer.schema.table", outer) is result

    assert current_catalog == "original"
    assert switch.call_args_list == [
        call("outer"),
        call("inner"),
        call("outer"),
        call("original"),
    ]


@pytest.mark.parametrize("operation_fails", [True, False])
def test_set_catalog_restore_failure(make_mocked_engine_adapter, mocker, operation_fails):
    adapter = make_mocked_engine_adapter(CatalogAdapter)
    mocker.patch.object(adapter, "get_current_catalog", return_value="original")
    operation_error = RuntimeError("operation failed")
    restore_error = RuntimeError("restore failed")
    switch = mocker.patch.object(adapter, "set_current_catalog", side_effect=[None, restore_error])
    callback = mocker.Mock(side_effect=operation_error if operation_fails else None)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.table_operation("temporary.schema.table", callback)

    assert exc_info.value is restore_error
    assert exc_info.value.__context__ is (operation_error if operation_fails else None)
    assert switch.call_args_list == [call("temporary"), call("original")]
