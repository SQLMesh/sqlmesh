import typing as t
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tenacity import retry, stop_after_attempt

import re
import pandas as pd  # noqa: TID253
import pytest
import sqlglot
from pytest_mock.plugin import MockerFixture
from sqlglot import exp
from sqlglot import exp as expressions
from sqlglot.expressions import SQLGLOT_META, to_table
from sqlglot.optimizer.pushdown_projections import SELECT_ALL

import tests.utils.test_date as test_date
from sqlmesh.core.dialect import normalize_model_name
from sqlmesh.core import constants as c
from sqlmesh.core.macros import RuntimeStage
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.metaprogramming import (
    Executable,
    ExecutableKind,
    _dict_sort,
    _resolve_import_module,
    build_env,
    func_globals,
    normalize_source,
    prepare_env,
    print_exception,
    serialize_env,
)


def test_print_exception(mocker: MockerFixture):
    out_mock = mocker.Mock()

    test_env = {
        "test_fun": Executable(
            name="test_func",
            payload="""def test_fun():
    raise RuntimeError("error")""",
            path="/test/path.py",
        ),
    }
    env = prepare_env(test_env)
    try:
        eval("test_fun()", env)
    except Exception as ex:
        print_exception(ex, test_env, out_mock)

    expected_message = r"""  File ".*?.tests.utils.test_metaprogramming\.py", line 49, in test_print_exception
    eval\("test_fun\(\)", env\).*

  File '/test/path.py' \(or imported file\), line 2, in test_fun
    def test_fun\(\):
        raise RuntimeError\("error"\)
      RuntimeError: error
"""
    actual_message = out_mock.write.call_args_list[0][0][0]
    assert isinstance(actual_message, str)

    expected_message = "".join(expected_message.split())
    actual_message = "".join(actual_message.split())

    assert re.match(expected_message, actual_message)


X = 1
Y = 2
Z = 3
W = 0

my_lambda = lambda: print("z")  # noqa: E731

KLASS_X = 1
KLASS_Y = 2
KLASS_Z = 3


@dataclass
class DataClass:
    x: int


class ReferencedClass:
    def __init__(self, value: int):
        self.value = value

    def get_value(self) -> int:
        return self.value


class MyClass:
    def __init__(self, x: int):
        self.helper = ReferencedClass(x * 2)

    @staticmethod
    def foo():
        return KLASS_X

    @classmethod
    def bar(cls):
        return KLASS_Y

    def baz(self):
        return KLASS_Z

    def use_referenced(self, value: int) -> int:
        ref = ReferencedClass(value)
        return ref.get_value()

    def compute_with_reference(self) -> int:
        return self.helper.get_value() + 10


def other_func(a: int) -> int:
    import sqlglot

    sqlglot.parse_one("1")
    pd.DataFrame([{"x": 1}])
    to_table("y")
    my_lambda()  # type: ignore
    obj = MyClass(a)
    return X + a + W + obj.compute_with_reference()


@contextmanager
def sample_context_manager():
    yield


@retry(stop=stop_after_attempt(3))
def fetch_data():
    return "'test data'"


def custom_decorator(_func):
    def wrapper(*args, **kwargs):
        return _func(*args, **kwargs)

    return wrapper


@custom_decorator
def function_with_custom_decorator():
    return


def main_func(y: int, foo=exp.true(), *, bar=expressions.Literal.number(1) + 2) -> int:
    """DOC STRING"""
    sqlglot.parse_one("1")
    MyClass(47)
    DataClass(x=y)
    normalize_model_name("test" + SQLGLOT_META)
    fetch_data()
    function_with_custom_decorator()

    def closure(z: int) -> int:
        return z + Z

    with sample_context_manager():
        pass

    return closure(y) + other_func(Y)


def macro1() -> str:
    print("macro1 hello there")
    print(RuntimeStage.CREATING)
    return "1"


def macro2() -> str:
    print("macro2 hello there")
    print(RuntimeStage.LOADING)
    return "2"


def test_func_globals() -> None:
    assert func_globals(main_func) == {
        "Y": 2,
        "Z": 3,
        "DataClass": DataClass,
        "MyClass": MyClass,
        "normalize_model_name": normalize_model_name,
        "other_func": other_func,
        "sqlglot": sqlglot,
        "exp": exp,
        "expressions": exp,
        "fetch_data": fetch_data,
        "sample_context_manager": sample_context_manager,
        "function_with_custom_decorator": function_with_custom_decorator,
        "SQLGLOT_META": SQLGLOT_META,
    }
    assert func_globals(other_func) == {
        "X": 1,
        "W": 0,
        "MyClass": MyClass,
        "my_lambda": my_lambda,
        "pd": pd,
        "to_table": to_table,
    }

    def closure_test() -> t.Callable:
        y = 1

        def closure() -> int:
            return main_func(y)

        return closure

    assert func_globals(closure_test()) == {
        "main_func": main_func,
        "y": 1,
    }


def test_normalize_source() -> None:
    assert (
        normalize_source(main_func)
        == """def main_func(y: int, foo=exp.true(), *, bar=expressions.Literal.number(1) + 2
    ):
    sqlglot.parse_one('1')
    MyClass(47)
    DataClass(x=y)
    normalize_model_name('test' + SQLGLOT_META)
    fetch_data()
    function_with_custom_decorator()

    def closure(z: int):
        return z + Z
    with sample_context_manager():
        pass
    return closure(y) + other_func(Y)"""
    )

    assert (
        normalize_source(other_func)
        == """def other_func(a: int):
    import sqlglot
    sqlglot.parse_one('1')
    pd.DataFrame([{'x': 1}])
    to_table('y')
    my_lambda()
    obj = MyClass(a)
    return X + a + W + obj.compute_with_reference()"""
    )


def test_serialize_env_error() -> None:
    with pytest.raises(SQLMeshError):
        # pretend to be the module pandas
        serialize_env({"test_date": (test_date, None)}, path=Path("tests/utils"))

    with pytest.raises(SQLMeshError):
        serialize_env({"select_all": (SELECT_ALL, None)}, path=Path("tests/utils"))


def test_serialize_env() -> None:
    path = Path("tests/utils")
    env: t.Dict[str, t.Tuple[t.Any, t.Optional[bool]]] = {}

    build_env(main_func, env=env, name="MAIN", path=path)
    serialized_env = serialize_env(env, path=path)  # type: ignore
    assert prepare_env(serialized_env)

    expected_env = {
        "MAIN": Executable(
            name="main_func",
            alias="MAIN",
            path="test_metaprogramming.py",
            payload="""def main_func(y: int, foo=exp.true(), *, bar=expressions.Literal.number(1) + 2
    ):
    sqlglot.parse_one('1')
    MyClass(47)
    DataClass(x=y)
    normalize_model_name('test' + SQLGLOT_META)
    fetch_data()
    function_with_custom_decorator()

    def closure(z: int):
        return z + Z
    with sample_context_manager():
        pass
    return closure(y) + other_func(Y)""",
        ),
        "X": Executable(payload="1", kind=ExecutableKind.VALUE),
        "Y": Executable(payload="2", kind=ExecutableKind.VALUE),
        "Z": Executable(payload="3", kind=ExecutableKind.VALUE),
        "W": Executable(payload="0", kind=ExecutableKind.VALUE),
        "_GeneratorContextManager": Executable(
            payload="from contextlib import _GeneratorContextManager", kind=ExecutableKind.IMPORT
        ),
        "contextmanager": Executable(
            payload="from contextlib import contextmanager", kind=ExecutableKind.IMPORT
        ),
        "KLASS_X": Executable(payload="1", kind=ExecutableKind.VALUE),
        "KLASS_Y": Executable(payload="2", kind=ExecutableKind.VALUE),
        "KLASS_Z": Executable(payload="3", kind=ExecutableKind.VALUE),
        "to_table": Executable(
            kind=ExecutableKind.IMPORT,
            payload="from sqlglot.expressions import to_table",
        ),
        "DataClass": Executable(
            kind=ExecutableKind.DEFINITION,
            name="DataClass",
            path="test_metaprogramming.py",
            payload="""@dataclass
class DataClass:
    x: int""",
        ),
        "MyClass": Executable(
            kind=ExecutableKind.DEFINITION,
            name="MyClass",
            path="test_metaprogramming.py",
            payload="""class MyClass:

    def __init__(self, x: int):
        self.helper = ReferencedClass(x * 2)

    @staticmethod
    def foo():
        return KLASS_X

    @classmethod
    def bar(cls):
        return KLASS_Y

    def baz(self):
        return KLASS_Z

    def use_referenced(self, value: int):
        ref = ReferencedClass(value)
        return ref.get_value()

    def compute_with_reference(self):
        return self.helper.get_value() + 10""",
        ),
        "ReferencedClass": Executable(
            kind=ExecutableKind.DEFINITION,
            name="ReferencedClass",
            path="test_metaprogramming.py",
            payload="""class ReferencedClass:

    def __init__(self, value: int):
        self.value = value

    def get_value(self):
        return self.value""",
        ),
        "dataclass": Executable(
            payload="from dataclasses import dataclass", kind=ExecutableKind.IMPORT
        ),
        "pd": Executable(payload="import pandas as pd", kind=ExecutableKind.IMPORT),
        "sqlglot": Executable(kind=ExecutableKind.IMPORT, payload="import sqlglot"),
        "exp": Executable(kind=ExecutableKind.IMPORT, payload="import sqlglot.expressions as exp"),
        "expressions": Executable(
            kind=ExecutableKind.IMPORT, payload="import sqlglot.expressions as expressions"
        ),
        "func": Executable(
            payload="""@contextmanager
def sample_context_manager():
    yield""",
            name="sample_context_manager",
            path="test_metaprogramming.py",
            alias="func",
        ),
        "my_lambda": Executable(
            name="my_lambda",
            path="test_metaprogramming.py",
            payload="my_lambda = lambda : print('z')",
        ),
        "normalize_model_name": Executable(
            payload="from sqlmesh.core.dialect import normalize_model_name",
            kind=ExecutableKind.IMPORT,
        ),
        "other_func": Executable(
            name="other_func",
            path="test_metaprogramming.py",
            payload="""def other_func(a: int):
    import sqlglot
    sqlglot.parse_one('1')
    pd.DataFrame([{'x': 1}])
    to_table('y')
    my_lambda()
    obj = MyClass(a)
    return X + a + W + obj.compute_with_reference()""",
        ),
        "sample_context_manager": Executable(
            payload="""@contextmanager
def sample_context_manager():
    yield""",
            name="sample_context_manager",
            path="test_metaprogramming.py",
        ),
        "wraps": Executable(payload="from functools import wraps", kind=ExecutableKind.IMPORT),
        "functools": Executable(payload="import functools", kind=ExecutableKind.IMPORT),
        "retry": Executable(payload="from tenacity import retry", kind=ExecutableKind.IMPORT),
        "stop_after_attempt": Executable(
            payload="from tenacity.stop import stop_after_attempt", kind=ExecutableKind.IMPORT
        ),
        "wrapped_f": Executable(
            payload='''@retry(stop=stop_after_attempt(3))
def fetch_data():
    return "'test data'"''',
            name="fetch_data",
            path="test_metaprogramming.py",
            alias="wrapped_f",
        ),
        "fetch_data": Executable(
            payload='''@retry(stop=stop_after_attempt(3))
def fetch_data():
    return "'test data'"''',
            name="fetch_data",
            path="test_metaprogramming.py",
        ),
        "f": Executable(
            payload='''@retry(stop=stop_after_attempt(3))
def fetch_data():
    return "'test data'"''',
            name="fetch_data",
            path="test_metaprogramming.py",
            alias="f",
        ),
        "function_with_custom_decorator": Executable(
            name="wrapper",
            path="test_metaprogramming.py",
            payload="""def wrapper(*args, **kwargs):
    return _func(*args, **kwargs)""",
            alias="function_with_custom_decorator",
        ),
        "custom_decorator": Executable(
            name="custom_decorator",
            path="test_metaprogramming.py",
            payload="""def custom_decorator(_func):

    def wrapper(*args, **kwargs):
        return _func(*args, **kwargs)
    return wrapper""",
        ),
        "_func": Executable(
            name="function_with_custom_decorator",
            path="test_metaprogramming.py",
            payload="""@custom_decorator
def function_with_custom_decorator():
    return""",
            alias="_func",
        ),
        "SQLGLOT_META": Executable.value("sqlglot.meta"),
    }

    assert all(not is_metadata for (_, is_metadata) in env.values())
    assert serialized_env == expected_env

    # Annotate the entrypoint as "metadata only" to show how it propagates
    setattr(main_func, c.SQLMESH_METADATA, True)

    env = {}

    build_env(main_func, env=env, name="MAIN", path=path)
    serialized_env = serialize_env(env, path=path)  # type: ignore
    assert prepare_env(serialized_env)

    expected_env = {k: Executable(**v.dict(), is_metadata=True) for k, v in expected_env.items()}

    # Every object is treated as "metadata only", transitively
    assert all(is_metadata for (_, is_metadata) in env.values())
    assert serialized_env == expected_env

    # Check that class references inside init are captured
    init_globals = func_globals(MyClass.__init__)
    assert "ReferencedClass" in init_globals

    env = {}
    build_env(other_func, env=env, name="other_func_test", path=path)
    serialized_env = serialize_env(env, path=path)

    assert "MyClass" in serialized_env
    assert "ReferencedClass" in serialized_env

    prepared_env = prepare_env(serialized_env)
    result = eval("other_func_test(2)", prepared_env)
    assert result == 17


def test_serialize_env_with_enum_import_appearing_in_two_functions() -> None:
    path = Path("tests/utils")
    env: t.Dict[str, t.Tuple[t.Any, t.Optional[bool]]] = {}

    build_env(macro1, env=env, name="macro1", path=path)
    build_env(macro2, env=env, name="macro2", path=path)

    serialized_env = serialize_env(env, path=path)  # type: ignore
    assert prepare_env(serialized_env)

    expected_env = {
        "RuntimeStage": Executable(
            payload="from sqlmesh.core.macros import RuntimeStage", kind=ExecutableKind.IMPORT
        ),
        "macro1": Executable(
            payload="""def macro1():
    print('macro1 hello there')
    print(RuntimeStage.CREATING)
    return '1'""",
            name="macro1",
            path="test_metaprogramming.py",
        ),
        "macro2": Executable(
            payload="""def macro2():
    print('macro2 hello there')
    print(RuntimeStage.LOADING)
    return '2'""",
            name="macro2",
            path="test_metaprogramming.py",
        ),
    }

    assert serialized_env == expected_env


def test_dict_sort_basic_types():
    """Test dict_sort with basic Python types."""
    # Test basic types that should use standard repr
    assert _dict_sort(42) == "42"
    assert _dict_sort("hello") == "'hello'"
    assert _dict_sort(True) == "True"
    assert _dict_sort(None) == "None"
    assert _dict_sort(3.14) == "3.14"


def test_dict_sort_dict_ordering():
    """Test that dict_sort produces consistent output for dicts with different key ordering."""
    # Same dict with different key ordering
    dict1 = {"c": 3, "a": 1, "b": 2}
    dict2 = {"a": 1, "b": 2, "c": 3}
    dict3 = {"b": 2, "c": 3, "a": 1}

    repr1 = _dict_sort(dict1)
    repr2 = _dict_sort(dict2)
    repr3 = _dict_sort(dict3)

    # All should produce the same representation
    assert repr1 == repr2 == repr3
    assert repr1 == "{'a': 1, 'b': 2, 'c': 3}"


def test_dict_sort_mixed_key_types():
    """Test dict_sort with mixed key types (strings and numbers)."""
    dict1 = {42: "number", "string": "text", 1: "one"}
    dict2 = {"string": "text", 1: "one", 42: "number"}

    repr1 = _dict_sort(dict1)
    repr2 = _dict_sort(dict2)

    # Should produce consistent ordering despite mixed key types
    assert repr1 == repr2
    # Numbers come before strings when sorting by string representation
    assert repr1 == "{1: 'one', 42: 'number', 'string': 'text'}"


def test_dict_sort_nested_structures():
    """Test dict_sort with deeply nested dictionaries."""
    nested1 = {"outer": {"z": 26, "a": 1}, "list": [3, {"y": 2, "x": 1}], "simple": "value"}

    nested2 = {"simple": "value", "list": [3, {"x": 1, "y": 2}], "outer": {"a": 1, "z": 26}}

    repr1 = _dict_sort(nested1)
    repr2 = _dict_sort(nested2)

    assert repr1 != repr2
    # Verify structure is maintained with sorted keys
    expected1 = "{'list': [3, {'y': 2, 'x': 1}], 'outer': {'z': 26, 'a': 1}, 'simple': 'value'}"
    expected2 = "{'list': [3, {'x': 1, 'y': 2}], 'outer': {'a': 1, 'z': 26}, 'simple': 'value'}"
    assert repr1 == expected1
    assert repr2 == expected2


def test_dict_sort_lists_and_tuples():
    """Test dict_sort preserves order for lists/tuples and doesn't sort nested dicts."""
    # Lists should be unchanged
    list_with_dicts = [{"z": 26, "a": 1}, {"y": 25, "b": 2}]
    list_repr = _dict_sort(list_with_dicts)
    expected_list = "[{'z': 26, 'a': 1}, {'y': 25, 'b': 2}]"
    assert list_repr == expected_list

    # Tuples should be unchanged
    tuple_with_dicts = ({"z": 26, "a": 1}, {"y": 25, "b": 2})
    tuple_repr = _dict_sort(tuple_with_dicts)
    expected_tuple = "({'z': 26, 'a': 1}, {'y': 25, 'b': 2})"
    assert tuple_repr == expected_tuple


def test_dict_sort_empty_containers():
    """Test dict_sort with empty containers."""
    assert _dict_sort({}) == "{}"
    assert _dict_sort([]) == "[]"
    assert _dict_sort(()) == "()"


def test_dict_sort_special_characters():
    """Test dict_sort handles special characters correctly."""
    special_dict = {
        "quotes": "text with 'single' and \"double\" quotes",
        "unicode": "unicode: ñáéíóú",
        "newlines": "text\nwith\nnewlines",
        "backslashes": "path\\to\\file",
    }

    result = _dict_sort(special_dict)

    # Should be valid Python that can be evaluated
    reconstructed = eval(result)
    assert reconstructed == special_dict

    # Should be deterministic - same input produces same output
    result2 = _dict_sort(special_dict)
    assert result == result2


def test_dict_sort_executable_integration():
    """Test that dict_sort works correctly with Executable.value()."""
    # Test the integration with Executable.value which is the main use case
    variables1 = {"env": "dev", "debug": True, "timeout": 30}
    variables2 = {"timeout": 30, "debug": True, "env": "dev"}

    exec1 = Executable.value(variables1, sort_root_dict=True)
    exec2 = Executable.value(variables2, sort_root_dict=True)

    # Should produce identical payloads despite different input ordering
    assert exec1.payload == exec2.payload
    assert exec1.payload == "{'debug': True, 'env': 'dev', 'timeout': 30}"

    # Should be valid Python
    reconstructed = eval(exec1.payload)
    assert reconstructed == variables1

    # non-deterministic repr should not change the payload
    exec3 = Executable.value(variables1)
    assert exec3.payload == "{'env': 'dev', 'debug': True, 'timeout': 30}"


def test_resolve_import_module():
    """Test that _resolve_import_module finds the shallowest public re-exporting module."""
    # to_table lives in sqlglot.expressions.builders but is re-exported from sqlglot.expressions
    assert _resolve_import_module(to_table, "to_table") == "sqlglot.expressions"

    # Objects whose __module__ is already the public module should be returned as-is
    assert _resolve_import_module(exp.Column, "Column") == "sqlglot.expressions"

    # Objects not re-exported by any parent should return the original module
    class _Local:
        __module__ = "some.deep.internal.module"

    assert _resolve_import_module(_Local, "_Local") == "some.deep.internal.module"
