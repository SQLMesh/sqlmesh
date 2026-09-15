from __future__ import annotations

from datetime import date, datetime

import pytest

from sqlmesh.utils.conversions import ensure_bool, make_serializable, try_str_to_bool


class TestTryStrToBool:
    """Tests for the try_str_to_bool function."""

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("TrUe", True),
            ("false", False),
            ("False", False),
            ("FALSE", False),
            ("FaLsE", False),
        ],
    )
    def test_boolean_strings(self, input_val: str, expected: bool) -> None:
        """Strings 'true' and 'false' (case-insensitive) convert to bool."""
        assert try_str_to_bool(input_val) is expected

    @pytest.mark.parametrize(
        "input_val",
        [
            "yes",
            "no",
            "1",
            "0",
            "",
            "truthy",
            "falsey",
            "t",
            "f",
            "on",
            "off",
        ],
    )
    def test_non_boolean_strings_pass_through(self, input_val: str) -> None:
        """Non-boolean strings are returned unchanged."""
        assert try_str_to_bool(input_val) == input_val

    def test_return_type_for_true(self) -> None:
        """Returns actual bool True, not truthy value."""
        result = try_str_to_bool("true")
        assert result is True
        assert isinstance(result, bool)

    def test_return_type_for_false(self) -> None:
        """Returns actual bool False, not falsey value."""
        result = try_str_to_bool("false")
        assert result is False
        assert isinstance(result, bool)


class TestEnsureBool:
    """Tests for the ensure_bool function."""

    def test_bool_true_passthrough(self) -> None:
        """Boolean True passes through unchanged."""
        assert ensure_bool(True) is True

    def test_bool_false_passthrough(self) -> None:
        """Boolean False passes through unchanged."""
        assert ensure_bool(False) is False

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ("true", True),
            ("True", True),
            ("false", False),
            ("False", False),
        ],
    )
    def test_boolean_strings(self, input_val: str, expected: bool) -> None:
        """String 'true'/'false' converts to corresponding bool."""
        assert ensure_bool(input_val) is expected

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ("yes", True),  # Non-empty string is truthy
            ("no", True),  # Non-empty string is truthy
            ("", False),  # Empty string is falsey
            ("0", True),  # String "0" is truthy (non-empty)
        ],
    )
    def test_other_strings_use_bool_conversion(self, input_val: str, expected: bool) -> None:
        """Non-boolean strings fall back to bool() conversion."""
        assert ensure_bool(input_val) is expected

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            (1, True),
            (0, False),
            (-1, True),
            (100, True),
        ],
    )
    def test_integers(self, input_val: int, expected: bool) -> None:
        """Integers convert via bool() - 0 is False, others True."""
        assert ensure_bool(input_val) is expected

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            (1.0, True),
            (0.0, False),
            (-0.5, True),
        ],
    )
    def test_floats(self, input_val: float, expected: bool) -> None:
        """Floats convert via bool() - 0.0 is False, others True."""
        assert ensure_bool(input_val) is expected

    @pytest.mark.parametrize(
        "input_val,expected",
        [
            ([], False),
            ([1], True),
            ({}, False),
            ({"a": 1}, True),
            (None, False),
        ],
    )
    def test_other_types(self, input_val: object, expected: bool) -> None:
        """Other types convert via bool()."""
        assert ensure_bool(input_val) is expected


class TestMakeSerializable:
    """Tests for the make_serializable function."""

    def test_date_to_isoformat(self) -> None:
        """date objects convert to ISO format string."""
        d = date(2024, 1, 15)
        assert make_serializable(d) == "2024-01-15"

    def test_datetime_to_isoformat(self) -> None:
        """datetime objects convert to ISO format string."""
        dt = datetime(2024, 1, 15, 10, 30, 45)
        assert make_serializable(dt) == "2024-01-15T10:30:45"

    def test_datetime_with_microseconds(self) -> None:
        """datetime with microseconds preserves precision."""
        dt = datetime(2024, 1, 15, 10, 30, 45, 123456)
        assert make_serializable(dt) == "2024-01-15T10:30:45.123456"

    def test_dict_recursive(self) -> None:
        """Dictionaries are processed recursively."""
        obj = {"date": date(2024, 1, 15), "name": "test"}
        result = make_serializable(obj)
        assert result == {"date": "2024-01-15", "name": "test"}

    def test_list_recursive(self) -> None:
        """Lists are processed recursively."""
        obj = [date(2024, 1, 15), "test", 123]
        result = make_serializable(obj)
        assert result == ["2024-01-15", "test", 123]

    def test_nested_structure(self) -> None:
        """Deeply nested structures are fully processed."""
        obj = {
            "dates": [date(2024, 1, 1), date(2024, 12, 31)],
            "nested": {"inner": {"dt": datetime(2024, 6, 15, 12, 0, 0)}},
        }
        result = make_serializable(obj)
        assert result == {
            "dates": ["2024-01-01", "2024-12-31"],
            "nested": {"inner": {"dt": "2024-06-15T12:00:00"}},
        }

    @pytest.mark.parametrize(
        "input_val",
        [
            "string",
            123,
            45.67,
            True,
            False,
            None,
        ],
    )
    def test_primitives_unchanged(self, input_val: object) -> None:
        """Primitive types pass through unchanged."""
        assert make_serializable(input_val) == input_val

    def test_empty_dict(self) -> None:
        """Empty dict returns empty dict."""
        assert make_serializable({}) == {}

    def test_empty_list(self) -> None:
        """Empty list returns empty list."""
        assert make_serializable([]) == []

    def test_dict_keys_unchanged(self) -> None:
        """Dictionary keys are not modified."""
        obj = {"key_with_date": date(2024, 1, 1)}
        result = make_serializable(obj)
        assert "key_with_date" in result
