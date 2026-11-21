import typing as t
import pytest
from functools import wraps
from sqlglot import expressions
from sqlglot.optimizer.annotate_types import annotate_types

from sqlmesh.core.console import set_console, get_console, TerminalConsole
from sqlmesh.core.engine_adapter import EngineAdapter, RedshiftEngineAdapter
from sqlmesh.utils import columns_to_types_all_known
from sqlmesh.utils.errors import SQLMeshError


@pytest.mark.parametrize(
    "columns_to_types, expected",
    [
        ({"a": expressions.DataType.build("INT"), "b": expressions.DataType.build("INT")}, True),
        (
            {"a": expressions.DataType.build("UNKNOWN"), "b": expressions.DataType.build("INT")},
            False,
        ),
        ({"a": expressions.DataType.build("NULL"), "b": expressions.DataType.build("INT")}, False),
        (
            {
                "a": expressions.DataType.build("INT"),
                "b": expressions.DataType.build(
                    "STRUCT<sub_a INT, sub_b INT, sub_c INT, sub_d INT>"
                ),
            },
            True,
        ),
        (
            {
                "a": expressions.DataType.build("INT"),
                "b": expressions.DataType.build(
                    "ARRAY<STRUCT<sub_a INT, sub_b INT, sub_c INT, sub_d INT>>"
                ),
            },
            True,
        ),
        (
            {
                "a": expressions.DataType.build("INT"),
                "b": expressions.DataType.build(
                    "ARRAY<STRUCT<sub_a INT, sub_b INT, sub_c INT, sub_d UNKNOWN>>"
                ),
            },
            False,
        ),
        (
            {
                "a": expressions.DataType.build("INT"),
                "b": expressions.DataType.build(
                    "ARRAY<STRUCT<sub_a INT, sub_b INT, sub_c INT, sub_d UNKNOWN>>"
                ),
            },
            False,
        ),
        (
            {
                "a": expressions.DataType.build("INT"),
                "b": expressions.DataType.build("MAP<INT, STRING>"),
            },
            True,
        ),
        (
            {
                "a": expressions.DataType.build("INT"),
                "b": expressions.DataType.build("MAP<INT, UNKNOWN>"),
            },
            False,
        ),
        (
            {"a": annotate_types(expressions.DataType.build("VARCHAR(MAX)", dialect="redshift"))},
            True,
        ),
        ({}, False),
    ],
)
def test_columns_to_types_all_known(columns_to_types, expected) -> None:
    assert columns_to_types_all_known(columns_to_types) == expected


@pytest.mark.parametrize("adapter_cls", [EngineAdapter, RedshiftEngineAdapter])
def test_create_table_empty_columns_raises(
    make_mocked_engine_adapter: t.Callable, adapter_cls: t.Type[EngineAdapter]
) -> None:
    adapter = make_mocked_engine_adapter(adapter_cls)
    with pytest.raises(
        SQLMeshError,
        match="Cannot create a table without knowing the column types",
    ):
        adapter.create_table("s.t", {}, exists=False)

    adapter.cursor.execute.assert_not_called()


def use_terminal_console(func):
    @wraps(func)
    def test_wrapper(*args, **kwargs):
        orig_console = get_console()
        try:
            new_console = TerminalConsole()
            new_console.console.width = 80
            new_console.console.no_color = True
            set_console(new_console)
            func(*args, **kwargs)
        finally:
            set_console(orig_console)

    return test_wrapper
