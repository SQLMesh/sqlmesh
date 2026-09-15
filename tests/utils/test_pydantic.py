import typing as t
import pytest
from functools import cached_property

import pydantic

from sqlmesh.utils.date import TimeLike, to_date, to_datetime
from sqlmesh.utils.pydantic import (
    PydanticModel,
    get_concrete_types_from_typehint,
    validation_error_message,
)


def test_datetime_date_serialization() -> None:
    class Test(PydanticModel):
        ds: TimeLike

    target_ds = "2022-01-01"

    deserialized_date = Test.parse_raw(Test(ds=to_date(target_ds)).json())
    deserialized_datetime = Test.parse_raw(Test(ds=to_datetime(target_ds)).json())
    assert deserialized_date.ds == to_date(target_ds)
    assert deserialized_datetime.ds == to_datetime("2022-01-01T00:00:00+00:00")


def test_pydantic_2_equality() -> None:
    class TestModel(PydanticModel):
        name: str

        @cached_property
        def private(self) -> str:
            return "should be ignored"

    model_a = TestModel(name="a")
    model_a_duplicate = TestModel(name="a")
    assert model_a == model_a_duplicate
    model_b = TestModel(name="b")
    assert model_a != model_b


def test_pydantic_2_hash() -> None:
    class TestModel(PydanticModel):
        name: str

        @cached_property
        def private(self) -> str:
            return "should be ignored"

    class TestModel2(PydanticModel):
        name: str
        field2: str = "test"

        @cached_property
        def private(self) -> str:
            return "should be ignored"

    model_a = TestModel(name="a")
    model_a_duplicate = TestModel(name="a")
    assert hash(model_a) == hash(model_a_duplicate)

    model_2_a = TestModel2(name="a")
    model_2_b = TestModel2(name="a")
    assert hash(model_2_a) == hash(model_2_b)
    assert hash(model_a) != hash(model_2_a)


def test_pydantic_dict_default_args_override() -> None:
    class TestModel(PydanticModel):
        name: str

    assert TestModel(name="foo").dict(by_alias=True)


@pytest.mark.parametrize(
    "input,output",
    [
        (t.Dict[str, t.Any], {dict}),
        (dict, {dict}),
        (t.List[str], {list}),
        (list, {list}),
        (t.Tuple[str, ...], {tuple}),
        (tuple, {tuple}),
        (t.Set[str], {set}),
        (set, {set}),
        (t.Optional[t.Dict[str, t.Any]], {dict, type(None)}),
        (t.Optional[t.List[str]], {list, type(None)}),
        (
            t.Union[str, t.List[str], t.Dict[str, t.Any], t.Optional[t.Set[str]]],
            {str, list, dict, set, type(None)},
        ),
    ],
)
def test_get_concrete_types_from_typehint(input: t.Any, output: set[type]) -> None:
    assert get_concrete_types_from_typehint(input) == output


def test_validation_error_message_with_list_index_location() -> None:
    class Inner(PydanticModel):
        x: int

    class Outer(PydanticModel):
        items: t.List[Inner]

    with pytest.raises(pydantic.ValidationError) as ex:
        Outer(items=[{"x": "not_an_int"}])

    # The error is located at a list index, so pydantic's loc mixes str field
    # names with an int sequence index (e.g. ("items", 0, "x")).
    message = validation_error_message(ex.value, "Invalid config:")
    assert "Invalid field 'items.0.x'" in message
