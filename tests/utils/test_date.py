import typing as t
from datetime import date, datetime

import pytest
import time_machine
from sqlglot import exp
import pandas as pd  # noqa: TID253

from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.date import (
    UTC,
    TimeLike,
    date_dict,
    format_tz_datetime,
    is_categorical_relative_expression,
    is_relative,
    make_inclusive,
    parse_time_zone,
    to_datetime,
    to_time_column,
    to_timestamp,
    to_ts,
    to_tstz,
)


def test_to_datetime() -> None:
    target = datetime(2020, 1, 1).replace(tzinfo=UTC)
    assert to_datetime("2020-01-01 00:00:00") == target
    assert to_datetime("2020-01-01T00:00:00") == target
    assert to_datetime("2020-01-01T05:00:00+05:00") == target
    assert to_datetime("2020-01-01") == target
    assert to_datetime(datetime(2020, 1, 1)) == target
    assert to_datetime(1577836800000) == target
    assert to_datetime(20200101) == target
    assert to_datetime("20200101") == target
    assert to_datetime("0") == datetime(1970, 1, 1, tzinfo=UTC)

    target = datetime(1971, 1, 1).replace(tzinfo=UTC)
    assert to_datetime("1971-01-01") == target
    assert to_datetime("31536000000") == target


@pytest.mark.parametrize(
    "expression, result",
    [
        ("1 second ago", datetime(2023, 1, 20, 12, 29, 59, tzinfo=UTC)),
        ("1 minute ago", datetime(2023, 1, 20, 12, 29, 00, tzinfo=UTC)),
        ("1 hour ago", datetime(2023, 1, 20, 11, 30, 00, tzinfo=UTC)),
        ("1 day ago", datetime(2023, 1, 19, 00, 00, 00, tzinfo=UTC)),
        ("1 week ago", datetime(2023, 1, 13, 00, 00, 00, tzinfo=UTC)),
        ("1 month ago", datetime(2022, 12, 20, 00, 00, 00, tzinfo=UTC)),
        ("1 year ago", datetime(2022, 1, 20, 00, 00, 00, tzinfo=UTC)),
        ("1 decade ago", datetime(2013, 1, 20, 00, 00, 00, tzinfo=UTC)),
        ("3 days 2 hours ago", datetime(2023, 1, 17, 10, 30, 00, tzinfo=UTC)),
        ("2 years 5 second ago", datetime(2021, 1, 20, 12, 29, 55, tzinfo=UTC)),
        ("24 hours ago", datetime(2023, 1, 19, 12, 30, 00, tzinfo=UTC)),
        ("1 year 5 days ago", datetime(2022, 1, 15, 00, 00, 00, tzinfo=UTC)),
        ("yesterday", datetime(2023, 1, 19, 00, 00, 00, tzinfo=UTC)),
        ("today", datetime(2023, 1, 20, 00, 00, 00, tzinfo=UTC)),
        ("tomorrow", datetime(2023, 1, 21, 00, 00, 00, tzinfo=UTC)),
    ],
)
def test_to_datetime_with_expressions(expression, result) -> None:
    with time_machine.travel("2023-01-20 12:30:30 UTC", tick=False):
        assert to_datetime(expression) == result


@time_machine.travel("2023-01-20 12:30:30 UTC", tick=False)
def test_to_datetime_with_relative_tz() -> None:
    la_tz = parse_time_zone("America/Los_Angeles")
    assert to_datetime("1 week ago") == datetime(2023, 1, 13, 0, 0, 0, tzinfo=UTC)
    assert to_datetime("1 week ago", relative_tz=la_tz) == datetime(
        2023, 1, 13, 8, 0, 0, tzinfo=UTC
    )
    assert to_datetime("2020-01-01", relative_tz=la_tz) == datetime(2020, 1, 1, tzinfo=UTC)


@time_machine.travel("2023-01-20 15:00:00 UTC", tick=False)
def test_to_datetime_with_relative_tz_tokyo() -> None:
    tokyo_tz = parse_time_zone("Asia/Tokyo")
    assert to_datetime("1 day ago", relative_tz=tokyo_tz) == datetime(
        2023, 1, 19, 15, 0, 0, tzinfo=UTC
    )


@time_machine.travel("2023-01-20 15:00:00 UTC", tick=False)
def test_date_dict_with_relative_tz() -> None:
    tokyo_tz = parse_time_zone("Asia/Tokyo")
    resp = date_dict(
        "2023-01-20 15:00:00",
        "1 day ago",
        "2023-01-20",
        relative_tz=tokyo_tz,
    )
    assert resp["start_dt"] == datetime(2023, 1, 19, 15, 0, 0, tzinfo=UTC)
    assert resp["start_ds"] == "2023-01-20"
    assert resp["start_date"] == date(2023, 1, 20)
    assert resp["execution_ds"] == "2023-01-20"
    assert resp["end_ds"] == "2023-01-20"


def test_parse_time_zone() -> None:
    assert parse_time_zone(None) is None
    assert parse_time_zone("UTC") is None
    la_tz = parse_time_zone("America/Los_Angeles")
    assert la_tz is not None
    assert la_tz.key == "America/Los_Angeles"

    with pytest.raises(ConfigError, match="valid IANA timezone"):
        parse_time_zone("Not/A_Timezone")


def test_date_dict_interval_bounds_use_utc_ds_with_relative_tz() -> None:
    tokyo_tz = parse_time_zone("Asia/Tokyo")
    interval_start = to_datetime("2023-01-20 00:00:00")
    resp = date_dict(
        "2023-01-20 15:00:00",
        interval_start,
        interval_start,
        relative_tz=tokyo_tz,
    )
    assert resp["start_ds"] == "2023-01-20"
    assert resp["start_dt"] == interval_start


def test_date_dict_absolute_start_uses_utc_ds_with_relative_tz() -> None:
    la_tz = parse_time_zone("America/Los_Angeles")
    resp = date_dict(
        "2023-01-20 12:00:00",
        "2023-01-13",
        "2023-01-20",
        relative_tz=la_tz,
    )
    assert resp["start_ds"] == "2023-01-13"
    assert resp["end_ds"] == "2023-01-20"


def test_to_timestamp_from_date_string() -> None:
    assert to_timestamp("2020-01-01") == 1577836800000


@time_machine.travel("2023-03-13 12:00:00 UTC", tick=False)
def test_to_datetime_relative_tz_spring_forward_dst() -> None:
    la_tz = parse_time_zone("America/Los_Angeles")
    assert to_datetime("1 day ago", relative_tz=la_tz) == datetime(2023, 3, 12, 8, 0, 0, tzinfo=UTC)


@time_machine.travel("2023-11-06 12:00:00 UTC", tick=False)
def test_to_datetime_relative_tz_fall_back_dst() -> None:
    la_tz = parse_time_zone("America/Los_Angeles")
    assert to_datetime("1 day ago", relative_tz=la_tz) == datetime(2023, 11, 5, 7, 0, 0, tzinfo=UTC)


@time_machine.travel("2023-01-20 12:30:00 UTC", tick=False)
def test_to_datetime_hour_relative_ignores_time_zone() -> None:
    la_tz = parse_time_zone("America/Los_Angeles")
    relative_base = to_datetime("2023-01-20 12:30:00")
    without_tz = to_datetime("2 hours ago", relative_base=relative_base)
    with_tz = to_datetime("2 hours ago", relative_base=relative_base, relative_tz=la_tz)
    assert with_tz == without_tz
    assert with_tz == datetime(2023, 1, 20, 10, 30, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "start_in, end_in, start_out, end_out",
    [
        ("2020-01-01", "2020-01-01", "2020-01-01", "2020-01-01 23:59:59.999999+00:00"),
        ("2020-01-01", date(2020, 1, 1), "2020-01-01", "2020-01-01 23:59:59.999999+00:00"),
        (
            date(2020, 1, 1),
            date(2020, 1, 1),
            "2020-01-01",
            "2020-01-01 23:59:59.999999+00:00",
        ),
        (
            "2020-01-01",
            "2020-01-01 12:00:00",
            "2020-01-01",
            "2020-01-01 11:59:59.999999+00:00",
        ),
        (
            "2020-01-01",
            to_datetime("2020-01-02"),
            "2020-01-01",
            "2020-01-01 23:59:59.999999+00:00",
        ),
    ],
)
def test_make_inclusive(start_in, end_in, start_out, end_out) -> None:
    assert make_inclusive(start_in, end_in) == (
        to_datetime(start_out),
        to_datetime(end_out),
    )


@pytest.mark.parametrize(
    "start_in, end_in, start_out, end_out, dialect",
    [
        ("2020-01-01", "2020-01-01", "2020-01-01", "2020-01-01 23:59:59.999999999+00:00", "tsql"),
        (
            "2020-01-01",
            date(2020, 1, 1),
            "2020-01-01",
            "2020-01-01 23:59:59.999999999+00:00",
            "tsql",
        ),
        (
            date(2020, 1, 1),
            date(2020, 1, 1),
            "2020-01-01",
            "2020-01-01 23:59:59.999999999+00:00",
            "tsql",
        ),
        (
            "2020-01-01",
            "2020-01-01 12:00:00",
            "2020-01-01",
            "2020-01-01 11:59:59.999999999+00:00",
            "tsql",
        ),
        (
            "2020-01-01",
            to_datetime("2020-01-02"),
            "2020-01-01",
            "2020-01-01 23:59:59.999999999+00:00",
            "tsql",
        ),
    ],
)
def test_make_inclusive_tsql(start_in, end_in, start_out, end_out, dialect) -> None:
    assert make_inclusive(start_in, end_in, "tsql") == (
        to_datetime(start_out),
        pd.Timestamp(end_out),
    )


@pytest.mark.parametrize(
    "expression, result",
    [
        ("1 second ago", False),
        ("1 minute ago", False),
        ("1 hour ago", False),
        ("1 day ago", True),
        ("1 week ago", True),
        ("1 month ago", True),
        ("1 year ago", True),
        ("1 decade ago", True),
        ("3 hours ago", False),
        ("24 hours ago", False),
        ("1 day 5 hours ago", False),
        ("1 year 5 minutes ago", False),
        ("2023-01-01", False),
        ("2023-01-01 12:00:00", False),
        ("yesterday", True),
        ("today", True),
        ("tomorrow", True),
    ],
)
def test_is_catagorical_relative_expression(expression, result):
    assert is_categorical_relative_expression(expression) == result


def test_to_ts():
    assert to_ts(datetime(2020, 1, 1).replace(tzinfo=UTC)) == "2020-01-01 00:00:00"
    assert to_ts(datetime(2020, 1, 1).replace(tzinfo=None)) == "2020-01-01 00:00:00"


def test_to_tstz():
    assert to_tstz(datetime(2020, 1, 1).replace(tzinfo=UTC)) == "2020-01-01 00:00:00+00:00"
    assert to_tstz(datetime(2020, 1, 1).replace(tzinfo=None)) == "2020-01-01 00:00:00+00:00"


@pytest.mark.parametrize(
    "time_column, time_column_type, dialect, time_column_format, result",
    [
        (
            exp.null(),
            exp.DataType.build("TIMESTAMP"),
            "",
            None,
            "CAST(NULL AS TIMESTAMP)",
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("DATE"),
            "",
            None,
            "CAST('2020-01-01' AS DATE)",
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("TIMESTAMPTZ"),
            "",
            None,
            "CAST('2020-01-01 00:00:00+00:00' AS TIMESTAMPTZ)",
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("TIMESTAMP"),
            "",
            None,
            "CAST('2020-01-01 00:00:00' AS TIMESTAMP)",
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("TEXT"),
            "",
            "%Y-%m-%dT%H:%M:%S%z",
            "'2020-01-01T00:00:00+0000'",
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("INT"),
            "",
            "%Y%m%d",
            "20200101",
        ),
        (
            "2020-01-01 00:00:00+00:00",
            exp.DataType.build("TIMESTAMPTZ"),
            "tsql",
            "%Y%m%d",
            "CAST('2020-01-01 00:00:00+00:00' AS DATETIMEOFFSET)",
        ),
        (
            pd.Timestamp("2020-01-01 00:00:00.1234567+00:00"),
            exp.DataType.build("DATETIME2", dialect="tsql"),
            "tsql",
            None,
            "CAST('2020-01-01 00:00:00.123456700' AS DATETIME2)",
        ),
    ],
)
def test_to_time_column(
    time_column: t.Union[TimeLike, exp.Null],
    time_column_type: exp.DataType,
    dialect: str,
    time_column_format: t.Optional[str],
    result: str,
):
    assert (
        to_time_column(time_column, time_column_type, dialect, time_column_format).sql(dialect)
        == result
    )


def test_date_dict():
    resp = date_dict("2020-01-02 01:00:00", "2020-01-01 00:00:00", "2020-01-02 00:00:00")
    assert resp == {
        "latest_dt": datetime(2020, 1, 2, 1, 0, 0, tzinfo=UTC),
        "execution_dt": datetime(2020, 1, 2, 1, 0, 0, tzinfo=UTC),
        "start_dt": datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC),
        "end_dt": datetime(2020, 1, 2, 0, 0, 0, tzinfo=UTC),
        "latest_dtntz": datetime(2020, 1, 2, 1, 0, 0, tzinfo=None),
        "execution_dtntz": datetime(2020, 1, 2, 1, 0, 0, tzinfo=None),
        "start_dtntz": datetime(2020, 1, 1, 0, 0, 0, tzinfo=None),
        "end_dtntz": datetime(2020, 1, 2, 0, 0, 0, tzinfo=None),
        "latest_date": date(2020, 1, 2),
        "execution_date": date(2020, 1, 2),
        "start_date": date(2020, 1, 1),
        "end_date": date(2020, 1, 2),
        "latest_ds": "2020-01-02",
        "execution_ds": "2020-01-02",
        "start_ds": "2020-01-01",
        "end_ds": "2020-01-02",
        "latest_ts": "2020-01-02 01:00:00",
        "execution_ts": "2020-01-02 01:00:00",
        "start_ts": "2020-01-01 00:00:00",
        "end_ts": "2020-01-02 00:00:00",
        "latest_tstz": "2020-01-02 01:00:00+00:00",
        "execution_tstz": "2020-01-02 01:00:00+00:00",
        "start_tstz": "2020-01-01 00:00:00+00:00",
        "end_tstz": "2020-01-02 00:00:00+00:00",
        "latest_epoch": 1577926800.0,
        "execution_epoch": 1577926800.0,
        "start_epoch": 1577836800.0,
        "end_epoch": 1577923200.0,
        "latest_millis": 1577926800000,
        "execution_millis": 1577926800000,
        "start_millis": 1577836800000,
        "end_millis": 1577923200000,
        "latest_hour": 1,
        "execution_hour": 1,
        "start_hour": 0,
        "end_hour": 0,
    }


@pytest.mark.parametrize(
    "start, end, expected_start_dt, expected_end_dt",
    [
        (
            "2020-01-01 00:00:00.1234567",
            "2020-01-02",
            to_datetime("2020-01-01 00:00:00.1234567+00:00"),
            pd.Timestamp("2020-01-02 23:59:59.999999999+00:00"),
        ),
        (
            "2020-01-01 00:00:00.1234567",
            "2020-01-02 00:00:00.1234567",
            to_datetime("2020-01-01 00:00:00.1234567+00:00"),
            pd.Timestamp("2020-01-02 00:00:00.123455999+00:00"),
        ),
        (
            "2020-01-01 00:00:00.1234567",
            "2020-01-02 00:00:00",
            to_datetime("2020-01-01 00:00:00.1234567+00:00"),
            pd.Timestamp("2020-01-01 23:59:59.999999999+00:00"),
        ),
    ],
)
def test_tsql_date_dict(start, end, expected_start_dt, expected_end_dt):
    resp = date_dict(
        "2020-01-02 01:00:00",
        *make_inclusive(start, end, "tsql"),
    )
    assert resp["start_dt"] == expected_start_dt
    assert resp["end_dt"] == expected_end_dt


def test_format_tz_datetime():
    test_datetime = to_datetime("2020-01-01 00:00:00")
    assert format_tz_datetime(test_datetime) == "2020-01-01 12:00AM UTC"
    assert format_tz_datetime(test_datetime, format_string=None) == "2020-01-01 00:00:00+00:00"


def test_is_relative():
    assert is_relative("1 week ago")
    assert is_relative("1 week")
    assert is_relative("1 day ago")
    assert is_relative("yesterday")

    assert not is_relative("2024-01-01")
    assert not is_relative("2024-01-01 01:02:03")
    assert not is_relative(to_datetime("2024-01-01 01:02:03"))
    assert not is_relative(to_timestamp("2024-01-01 01:02:03"))
    assert not is_relative(to_datetime("1 week ago"))
    assert not is_relative(to_timestamp("1 day ago"))
