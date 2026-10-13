import pandas as pd  # noqa: TID253
import pytest
from sqlglot import exp

from sqlmesh.core.model.seed import CsvSettings, Seed


def test_read():
    content = """key,value,ds,bool
1,one,2022-01-01,true
2,two,2022-01-02,false
3,three,2022-01-03,true
"""
    seed = Seed(content=content)
    # Since we provide "snowflake" as the dialect, all identifiers are expected to
    # be normalized according to its resolution rules, hence the uppercase names
    seed_reader = seed.reader(dialect="snowflake")

    assert seed_reader.columns_to_types == {
        "KEY": exp.DataType.build("bigint"),
        "VALUE": exp.DataType.build("text"),
        "DS": exp.DataType.build("text"),
        "BOOL": exp.DataType.build("boolean"),
    }
    expected_df = pd.DataFrame(
        data={
            "KEY": [1, 2, 3],
            "VALUE": ["one", "two", "three"],
            "DS": ["2022-01-01", "2022-01-02", "2022-01-03"],
            "BOOL": [True, False, True],
        }
    )
    dfs = seed_reader.read(batch_size=2)
    pd.testing.assert_frame_equal(next(dfs), expected_df.iloc[:2, :])
    pd.testing.assert_frame_equal(next(dfs), expected_df.iloc[2:, :])

    with pytest.raises(StopIteration):
        next(dfs)


def test_read_custom_settings():
    content = """key,value,ds
1,'one','2022-01-01'
2,'two','2022-01-02'
3,'three','2022-01-03'
"""
    seed = Seed(content=content)
    seed_reader = seed.reader(settings=CsvSettings(quotechar="'"))

    expected_df = pd.DataFrame(
        data={
            "key": [1, 2, 3],
            "value": ["one", "two", "three"],
            "ds": ["2022-01-01", "2022-01-02", "2022-01-03"],
        }
    )
    dfs = seed_reader.read()
    pd.testing.assert_frame_equal(next(dfs), expected_df)


def test_read_returns_independent_batches():
    content = """key,value
1,one
2,two
"""
    seed = Seed(content=content)
    seed_reader = seed.reader()

    batches = list(seed_reader.read(batch_size=1))
    batches[0].at[0, "value"] = "changed"

    assert [df["value"].tolist() for df in batches] == [["changed"], ["two"]]
    assert next(seed_reader.read())["value"].tolist() == ["one", "two"]


def test_column_hashes():
    content = """key,value,ds
1,one,2022-01-01
2,two,2022-01-02
3,three,2022-01-03
"""
    seed = Seed(content=content)
    assert seed.reader().column_hashes == {
        "key": "122302783",
        "value": "1969959181",
        "ds": "725407375",
    }

    content_column_changed = """key,value,ds
1,one,2022-01-01
2,two,2022-01-05
3,three,2022-01-03

"""
    seed_column_changed = Seed(content=content_column_changed)
    assert seed_column_changed.reader().column_hashes == {
        **seed.reader().column_hashes,
        "ds": "3396890652",
    }


def _declared(**types: str):
    return {name: exp.DataType.build(tpe) for name, tpe in types.items()}


def _values(series: pd.Series):
    """Series values with missing entries as None."""
    return [None if pd.isna(v) else v for v in series]


def test_declared_text_keeps_leading_zeros():
    seed = Seed(content="code,other,undeclared\n00123,1,007\n45958,2,8\n,3,9\n")
    df = next(seed.reader(columns_to_types=_declared(code="text", other="int")).read())

    assert _values(df["code"]) == ["00123", "45958", None]
    assert df["other"].dtype == "int64"
    assert df["other"].to_list() == [1, 2, 3]
    assert df["undeclared"].to_list() == [7, 8, 9]


@pytest.mark.parametrize("blank", [False, True])
def test_declared_text_keeps_boolean_like_values(blank):
    # pandas reads TRUE/FALSE as bools, in an object column when there are blanks
    seed = Seed(content="flag,other\nFALSE,1\nTRUE,2\n" + (",3\n" if blank else ""))
    df = next(seed.reader(columns_to_types=_declared(flag="text")).read())

    assert _values(df["flag"]) == ["FALSE", "TRUE"] + ([None] if blank else [])


def test_declared_int_with_blank_stays_integer():
    seed = Seed(content="n,big,ok\n1,9007199254740993,1\n,,2\n3,3,3\n")
    df = next(seed.reader(columns_to_types=_declared(n="int", big="bigint", ok="int")).read())

    assert df["n"].to_list() == [1, None, 3]
    assert [type(v) for v in df["n"]] == [int, type(None), int]
    assert df["big"].to_list() == [9007199254740993, None, 3]
    assert all(v is None or type(v) is int for v in df["big"])
    assert df["ok"].dtype == "int64"


@pytest.mark.parametrize("value", ["1.5", "1.0", "1e3"])
def test_declared_int_with_non_integer_literals_keeps_inferred_floats(value):
    seed = Seed(content=f"n,code\n{value},001\n,002\n3,003\n")
    df = next(seed.reader(columns_to_types=_declared(n="int", code="text")).read())
    expected = next(seed.reader().read())

    assert df["n"].dtype == "float64"
    assert _values(df["n"]) == _values(expected["n"])
    assert df["code"].to_list() == ["001", "002", "003"]


@pytest.mark.parametrize(
    "value",
    ["9223372036854775808", "99999999999999999999"],
)
@pytest.mark.parametrize("blank", [False, True])
def test_declared_int_out_of_int64_range_is_read_as_before(value, blank):
    seed = Seed(content=f"n,id\n{value},1\n{'' if blank else 5},2\n")
    declared = seed.reader(columns_to_types=_declared(n="bigint", id="int"))
    df = next(declared.read())
    expected = next(seed.reader().read())

    assert df["n"].to_list() == expected["n"].to_list()
    assert df["id"].to_list() == [1, 2]


def test_declared_columns_honor_csv_settings():
    seed = Seed(content="code|n\n00123|1\nNONE|\n")
    settings = CsvSettings(delimiter="|", na_values=["NONE"])
    df = next(
        seed.reader(settings=settings, columns_to_types=_declared(code="text", n="int")).read()
    )

    assert _values(df["code"]) == ["00123", None]
    assert _values(df["n"]) == [1, None]


def test_declared_columns_match_normalized_csv_headers():
    seed = Seed(content="Code,N\n001,1\n,2\n")
    reader = seed.reader(dialect="snowflake", columns_to_types=_declared(CODE="text", N="int"))
    df = next(reader.read())

    assert list(df.columns) == ["CODE", "N"]
    assert _values(df["CODE"]) == ["001", None]


def test_column_hashes_only_change_for_seeds_that_were_read_incorrectly():
    declared = _declared(id="int", name="text", code="text", n="int")

    unaffected = Seed(content="id,name\n1,alice\n2,bob\n")
    assert (
        unaffected.reader(columns_to_types=declared).column_hashes
        == unaffected.reader().column_hashes
    )

    # These seeds had wrong values (dropped leading zeros, integers read as floats), so their
    # hashes change once after upgrade, which is expected.
    for content in ["code\n00123\n45958\n", "n,k\n1,a\n,b\n3,c\n"]:
        affected = Seed(content=content)
        assert (
            affected.reader(columns_to_types=declared).column_hashes
            != affected.reader().column_hashes
        )
