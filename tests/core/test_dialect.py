import pytest
from sqlglot import Dialect, ParseError, exp, parse_one
from sqlglot.dialects.dialect import NormalizationStrategy

from sqlmesh.core.dialect import (
    JinjaQuery,
    JinjaStatement,
    Model,
    format_model_expressions,
    normalize_model_name,
    parse,
    select_from_values_for_batch_range,
    text_diff,
)
import sqlmesh.core.dialect as d
from sqlmesh.core.model import SqlModel, load_sql_based_model
from sqlmesh.core.config.connection import DIALECT_TO_TYPE

pytestmark = pytest.mark.dialect_isolated


def test_format_model_expressions():
    x = format_model_expressions(
        parse(
            """
    MODEL(
    name a.b, -- a
    kind full, -- b
    references (
     a,
     (b, c) as d,
     ),  -- c
       @macro_prop_with_comment(proper := 'foo'), -- k
     audits [
    not_null(columns=[
      foo_id,
      foo_normalised,
      bar_normalised,
      total_weight,
      overall_rank,
      cumulative_total_weight_share,
      market_rank,
      market_cumulative_total_weight_share,
      total_weight_decile,
      tier,
    ]),

    unique_values(columns=[foo_id]),

    accepted_range(column=foo_normalised, min_v=0, max_v=100),
    accepted_range(column=bar_normalised, min_v=0, max_v=100),
    accepted_range(column=total_weight, min_v=0, max_v=100),
    accepted_range(column=cumulative_total_weight_share, min_v=0, max_v=1),
    accepted_range(column=market_cumulative_total_weight_share, min_v=0, max_v=1),

    accepted_values(column=tier, is_in=['Tier 1', 'Tier 2', 'Tier 3', 'Long Tail']),
    accepted_values(column=total_weight_decile, is_in=['Decile_01', 'Decile_02','Decile_03','Decile_04','Decile_05','Decile_06','Decile_07','Decile_08','Decile_09','Decile_10']),
  ],
    )
    ;

    /* comment */
    @DEF(x
    , 1);

    SELECT
    *,
    CAST(a AS int),
    CAST(b AS int) AS b,
    CAST(c + 1 AS int) AS c,
    d::int,
    e::int as e,
    (f + 1)::int as f,
    sum(g + 1)::int as g,
    CAST(h AS int), -- h
    CAST(i AS int) AS i, -- i
    CAST(j + 1 AS int) AS j, -- j
    k::int, -- k
    l::int as l, -- l
    (m + 1)::int as m, -- m
    sum(n + 1)::int as n, -- n
    o,
    p + 1,
    CAST(x as int)::int,;

@IF(
    @runtime_stage = 'creating',
    GRANT SELECT ON foo.bar TO "bla"
)
    """
        )
    )
    assert (
        x
        == """MODEL (
  name a.b, /* a */
  kind FULL, /* b */
  references (a, (b, c) AS d), /* c */
  @macro_prop_with_comment(proper := 'foo'), /* k */
  audits ARRAY(
    NOT_NULL(
      columns = ARRAY(
        foo_id,
        foo_normalised,
        bar_normalised,
        total_weight,
        overall_rank,
        cumulative_total_weight_share,
        market_rank,
        market_cumulative_total_weight_share,
        total_weight_decile,
        tier
      )
    ),
    UNIQUE_VALUES(columns = ARRAY(foo_id)),
    ACCEPTED_RANGE(column = foo_normalised, min_v = 0, max_v = 100),
    ACCEPTED_RANGE(column = bar_normalised, min_v = 0, max_v = 100),
    ACCEPTED_RANGE(column = total_weight, min_v = 0, max_v = 100),
    ACCEPTED_RANGE(column = cumulative_total_weight_share, min_v = 0, max_v = 1),
    ACCEPTED_RANGE(column = market_cumulative_total_weight_share, min_v = 0, max_v = 1),
    ACCEPTED_VALUES(column = tier, is_in = ARRAY('Tier 1', 'Tier 2', 'Tier 3', 'Long Tail')),
    ACCEPTED_VALUES(
      column = total_weight_decile,
      is_in = ARRAY(
        'Decile_01',
        'Decile_02',
        'Decile_03',
        'Decile_04',
        'Decile_05',
        'Decile_06',
        'Decile_07',
        'Decile_08',
        'Decile_09',
        'Decile_10'
      )
    )
  )
);

/* comment */
@DEF(x, 1);

SELECT
  *,
  a::INT,
  b::INT AS b,
  CAST(c + 1 AS INT) AS c,
  d::INT,
  e::INT AS e,
  (
    f + 1
  )::INT AS f,
  SUM(g + 1)::INT AS g,
  h::INT, /* h */
  i::INT AS i, /* i */
  CAST(j + 1 AS INT) AS j, /* j */
  k::INT, /* k */
  l::INT AS l, /* l */
  (
    m + 1
  )::INT AS m, /* m */
  SUM(n + 1)::INT AS n, /* n */
  o,
  p + 1,
  x::INT::INT;

@IF(@runtime_stage = 'creating', GRANT SELECT ON foo.bar TO "bla")"""
    )

    x = format_model_expressions(
        parse(
            """
            MODEL(name a.b, kind FULL);
            JINJA_QUERY_BEGIN; /* comment */ SELECT * FROM x WHERE y = {{ 1 }}; /* comment */ JINJA_END;"""
        )
    )
    assert (
        x
        == """MODEL (
  name a.b,
  kind FULL
);

JINJA_QUERY_BEGIN;
/* comment */ SELECT * FROM x WHERE y = {{ 1 }}; /* comment */
JINJA_END;"""
    )

    x = format_model_expressions(
        parse(
            """
            MODEL(name a.b, kind FULL, dialect bigquery);
            SELECT SAFE_CAST('bla' AS INT64) AS FOO
            """
        ),
        dialect="bigquery",
    )
    assert (
        x
        == """MODEL (
  name a.b,
  kind FULL,
  dialect bigquery
);

SELECT
  SAFE_CAST('bla' AS INT64) AS FOO"""
    )

    x = format_model_expressions(
        parse(
            """
            MODEL(name a.b, kind FULL, dialect clickhouse);
            SELECT data.:String AS foo, CAST(1 AS INT) AS bar
            """
        ),
        dialect="clickhouse",
    )
    # JSONCast (e.g. `.:` syntax in ClickHouse) must not be written to `::`
    assert (
        x
        == """MODEL (
  name a.b,
  kind FULL,
  dialect clickhouse
);

SELECT
  data.:String AS foo,
  1::Int32 AS bar"""
    )

    x = format_model_expressions(
        parse(
            """
            MODEL(name foo);
            SELECT CAST(1 AS INT) AS bla
            """
        ),
        rewrite_casts=False,
    )
    assert (
        x
        == """MODEL (
  name foo
);

SELECT
  CAST(1 AS INT) AS bla"""
    )

    x = format_model_expressions(
        parse(
            """MODEL(name foo);
SELECT CAST(1 AS INT) AS bla;
            on_virtual_update_begin;
CREATE OR REPLACE VIEW test_view FROM demo_db.table;GRANT SELECT ON VIEW @this_model TO ROLE owner_name;
JINJA_STATEMENT_BEGIN; GRANT SELECT ON VIEW {{this_model}} TO ROLE admin;        JINJA_END;
    GRANT REFERENCES, SELECT ON FUTURE VIEWS IN DATABASE demo_db TO ROLE owner_name;
@resolve_parent_name('parent');GRANT SELECT ON VIEW demo_db.table /* sqlglot.meta replace=false */ TO ROLE admin;
ON_VIRTUAL_update_end;"""
        )
    )

    assert (
        x
        == """MODEL (
  name foo
);

SELECT
  1::INT AS bla;

ON_VIRTUAL_UPDATE_BEGIN;
CREATE OR REPLACE VIEW test_view AS
SELECT
  *
FROM demo_db.table;
GRANT SELECT ON VIEW @this_model TO ROLE owner_name;
JINJA_STATEMENT_BEGIN;
GRANT SELECT ON VIEW {{this_model}} TO ROLE admin;
JINJA_END;
GRANT REFERENCES, SELECT ON FUTURE VIEWS IN DATABASE demo_db TO ROLE owner_name;
@resolve_parent_name('parent');
GRANT SELECT ON VIEW demo_db.table /* sqlglot.meta replace=false */ TO ROLE admin;
ON_VIRTUAL_UPDATE_END;"""
    )


def test_macro_format():
    assert parse_one("@EACH(ARRAY(1,2), x -> x)").sql() == "@EACH(ARRAY(1, 2), x -> x)"
    assert parse_one("INTERVAL @x DAY").sql() == "INTERVAL @x DAY"
    assert parse_one("INTERVAL @'@{bar}' DAY").sql() == "INTERVAL @'@{bar}' DAY"
    assert parse_one("INTERVAL @x @y").sql() == "INTERVAL @x @y"
    assert parse_one("INTERVAL 1 @y").sql() == "INTERVAL '1' @y"


def test_format_body_macros():
    assert (
        format_model_expressions(
            parse(
                """
    Model ( name foo , @macro_dialect(), @properties_macro(prop_1 := 'max', prop_2 := 33));
    @WITH(TRUE) x AS (SELECT 1)
    SELECT col::int
    FROM foo
    WHERE @MY_MACRO() /* my macro comment */
@ORDER_BY(@include_order_by)
    @EACH( @columns,
    item -> @'@iteaoeuatnoehutoenahuoanteuhonateuhaoenthuaoentuhaeotnhaoem'),      @'@foo'
    """
            )
        )
        == """MODEL (
  name foo,
  @macro_dialect(),
  @properties_macro(prop_1 := 'max', prop_2 := 33)
);

@WITH(TRUE) x AS (
  SELECT
    1
)
SELECT
  col::INT
FROM foo
WHERE
  @MY_MACRO() /* my macro comment */
@ORDER_BY(@include_order_by)
  @EACH(@columns, item -> @'@iteaoeuatnoehutoenahuoanteuhonateuhaoenthuaoentuhaeotnhaoem'),
  @'@foo'"""
    )


def test_text_diff():
    assert """@@ -1,3 +1,3 @@

 SELECT
-  *
-FROM x
+  1
+FROM y""" in text_diff(parse("SELECT * FROM x"), parse("SELECT 1 FROM y"))


def test_parse():
    expressions = parse(
        """
        MODEL (
            kind full,
            dialect "hive",
        );

        CACHE TABLE x as SELECT 1 as Y;

        JINJA_QUERY_BEGIN;

        SELECT * FROM x WHERE y = {{ 1 }} ;

        JINJA_END;

        JINJA_STATEMENT_BEGIN;

        {{ side_effect() }};

        JINJA_END;
    """
    )

    assert len(expressions) == 4
    assert isinstance(expressions[0], Model)
    assert isinstance(expressions[1], exp.Cache)
    assert isinstance(expressions[2], JinjaQuery)
    assert isinstance(expressions[3], JinjaStatement)

    assert parse_one("{'a': 1}", read="duckdb").sql(dialect="duckdb") == "{'a': 1}"

    assert parse_one("metric") == exp.column("metric")
    assert parse_one("model(1, 2, 3)") == exp.func("model", 1, 2, 3)

    expressions = parse(
        """
        MODEL (
            kind full,
            dialect duckdb,
            grain metric,
        );

        SELECT 1 AS metric
        """
    )
    assert len(expressions) == 2
    assert isinstance(expressions[0], Model)
    assert isinstance(expressions[1], exp.Select)
    assert expressions[0].expressions[2].args["value"] == exp.to_identifier("metric")

    expressions = parse(
        """
        MODEL(
          name model1,
          dialect snowflake
        );

        SELECT a FROM @If(true, m2, m3)
        """,
    )
    assert len(expressions) == 2
    assert expressions[1].sql(dialect="snowflake") == "SELECT a FROM @IF(TRUE, m2, m3)"


def test_parse_jinja_with_semicolons():
    expressions = parse(
        """
        CREATE TABLE a as SELECT 1;
        CREATE TABLE b as SELECT 1;

        JINJA_STATEMENT_BEGIN;

        {% call set_sql_header(config) %}
            CREATE OR REPLACE TEMP MACRO add(a, b) AS a + b;
        {%- endcall %}

        JINJA_END;

        DROP TABLE a;
        DROP TABLE b;
    """
    )

    assert len(expressions) == 5
    assert isinstance(expressions[0], exp.Create)
    assert isinstance(expressions[1], exp.Create)
    assert isinstance(expressions[2], JinjaStatement)
    assert isinstance(expressions[3], exp.Drop)
    assert isinstance(expressions[4], exp.Drop)


def test_seed():
    expressions = parse(
        """
        MODEL (
            kind SEED (
                path '..\\..\\..\\data\\data.csv', -- c
            ),
        );
    """
    )
    assert len(expressions) == 1
    assert "../../../data/data.csv" in expressions[0].sql()
    assert (
        format_model_expressions(expressions)
        == """MODEL (
  kind SEED (
    path '../../../data/data.csv' /* c */
  )
)"""
    )


def test_select_from_values_for_batch_range_json():
    values = [(1, "2022-01-01", '{"foo":"bar"}'), (2, "2022-01-01", '{"foo":"qaz"}')]
    columns_to_types = {
        "id": exp.DataType.build("int"),
        "ds": exp.DataType.build("text"),
        "json_col": exp.DataType.build("json"),
    }

    assert select_from_values_for_batch_range(values, columns_to_types, 0, len(values)).sql() == (
        """SELECT CAST(id AS INT) AS id, CAST(ds AS TEXT) AS ds, CAST(json_col AS JSON) AS json_col """
        """FROM """
        """(VALUES (1, '2022-01-01', PARSE_JSON('{"foo":"bar"}')), (2, '2022-01-01', PARSE_JSON('{"foo":"qaz"}'))) """
        """AS t(id, ds, json_col)"""
    )

    assert select_from_values_for_batch_range([], columns_to_types, 0, 0).sql() == (
        "SELECT CAST(id AS INT) AS id, CAST(ds AS TEXT) AS ds, CAST(json_col AS JSON) AS json_col "
        "FROM (VALUES (CAST(NULL AS INT), CAST(NULL AS TEXT), CAST(NULL AS JSON))) AS t(id, ds, json_col) WHERE FALSE"
    )


def test_select_from_values_that_include_null():
    values = [(1, exp.null())]
    columns_to_types = {
        "id": exp.DataType.build("int", dialect="bigquery"),
        "ts": exp.DataType.build("timestamp", dialect="bigquery"),
    }

    values_expr = select_from_values_for_batch_range(values, columns_to_types, 0, len(values))
    assert values_expr.sql(dialect="bigquery") == (
        "SELECT CAST(id AS INT64) AS id, CAST(ts AS TIMESTAMP) AS ts FROM "
        "UNNEST([STRUCT(1 AS id, CAST(NULL AS TIMESTAMP) AS ts)]) AS t"
    )


@pytest.fixture(params=["mysql", "duckdb", "postgres", "snowflake"])
def normalization_dialect(request):
    if request.param == "duckdb":
        assert Dialect["duckdb"].NORMALIZATION_STRATEGY == NormalizationStrategy.CASE_INSENSITIVE
    elif request.param == "mysql":
        assert Dialect["mysql"].NORMALIZATION_STRATEGY == NormalizationStrategy.CASE_SENSITIVE
    elif request.param == "snowflake":
        assert Dialect["snowflake"].NORMALIZATION_STRATEGY == NormalizationStrategy.UPPERCASE
    elif request.param == "postgres":
        assert Dialect["postgres"].NORMALIZATION_STRATEGY == NormalizationStrategy.LOWERCASE
    return request.param


normalization_tests_fields = (
    "table, default_catalog, case_sensitive, case_insensitive, lowercase, uppercase"
)
normalization_tests = [
    ("table", None, '"table"', '"table"', '"table"', '"TABLE"'),
    ("db.table", None, '"db"."table"', '"db"."table"', '"db"."table"', '"DB"."TABLE"'),
    (
        "catalog.db.table",
        None,
        '"catalog"."db"."table"',
        '"catalog"."db"."table"',
        '"catalog"."db"."table"',
        '"CATALOG"."DB"."TABLE"',
    ),
    ("table", "catalog", '"table"', '"table"', '"table"', '"TABLE"'),
    (
        "db.table",
        "catalog",
        '"catalog"."db"."table"',
        '"catalog"."db"."table"',
        '"catalog"."db"."table"',
        '"CATALOG"."DB"."TABLE"',
    ),
    (
        "DB.TABLE",
        "CATALOG",
        '"CATALOG"."DB"."TABLE"',
        '"catalog"."db"."table"',
        '"catalog"."db"."table"',
        '"CATALOG"."DB"."TABLE"',
    ),
    ("tAble", None, '"tAble"', '"table"', '"table"', '"TABLE"'),
    (
        "Db.tAble",
        "CaTalog",
        '"CaTalog"."Db"."tAble"',
        '"catalog"."db"."table"',
        '"catalog"."db"."table"',
        '"CATALOG"."DB"."TABLE"',
    ),
    ('"tAble"', None, '"tAble"', '"table"', '"tAble"', '"tAble"'),
    (
        'Db."tAble"',
        '"CaTalog"',
        '"CaTalog"."Db"."tAble"',
        '"catalog"."db"."table"',
        '"CaTalog"."db"."tAble"',
        '"CaTalog"."DB"."tAble"',
    ),
]


@pytest.mark.parametrize(normalization_tests_fields, normalization_tests)
def test_normalize_model_name(
    table,
    default_catalog,
    case_sensitive,
    case_insensitive,
    lowercase,
    uppercase,
    normalization_dialect,
):
    if Dialect[normalization_dialect].NORMALIZATION_STRATEGY == NormalizationStrategy.UPPERCASE:
        expected = uppercase
    elif (
        Dialect[normalization_dialect].NORMALIZATION_STRATEGY
        == NormalizationStrategy.CASE_SENSITIVE
    ):
        expected = case_sensitive
    elif (
        Dialect[normalization_dialect].NORMALIZATION_STRATEGY
        == NormalizationStrategy.CASE_INSENSITIVE
    ):
        expected = case_insensitive
    else:
        expected = lowercase
    assert normalize_model_name(table, default_catalog, normalization_dialect) == expected


@pytest.mark.parametrize(normalization_tests_fields, normalization_tests)
def test_multiple_normalization(
    table,
    default_catalog,
    case_sensitive,
    case_insensitive,
    lowercase,
    uppercase,
    normalization_dialect,
):
    if Dialect[normalization_dialect].NORMALIZATION_STRATEGY == NormalizationStrategy.UPPERCASE:
        expected = uppercase
    elif (
        Dialect[normalization_dialect].NORMALIZATION_STRATEGY
        == NormalizationStrategy.CASE_SENSITIVE
    ):
        expected = case_sensitive
    elif (
        Dialect[normalization_dialect].NORMALIZATION_STRATEGY
        == NormalizationStrategy.CASE_INSENSITIVE
    ):
        expected = case_insensitive
    else:
        expected = lowercase
    kwargs = {"default_catalog": default_catalog, "dialect": normalization_dialect}
    assert (
        normalize_model_name(
            normalize_model_name(normalize_model_name(table, **kwargs), **kwargs), **kwargs
        )
        == expected
    )


@pytest.mark.parametrize(normalization_tests_fields, normalization_tests)
def test_model_normalization_multiple_serde(
    table,
    default_catalog,
    case_sensitive,
    case_insensitive,
    lowercase,
    uppercase,
    normalization_dialect,
):
    if Dialect[normalization_dialect].NORMALIZATION_STRATEGY == NormalizationStrategy.UPPERCASE:
        expected = uppercase
    elif (
        Dialect[normalization_dialect].NORMALIZATION_STRATEGY
        == NormalizationStrategy.CASE_SENSITIVE
    ):
        expected = case_sensitive
    elif (
        Dialect[normalization_dialect].NORMALIZATION_STRATEGY
        == NormalizationStrategy.CASE_INSENSITIVE
    ):
        expected = case_insensitive
    else:
        expected = lowercase
    expressions = parse(
        f"""
        MODEL (
            name {exp.maybe_parse(table, into=exp.Table).sql(dialect=normalization_dialect)},
            kind INCREMENTAL_BY_TIME_RANGE(
                time_column ds
            ),
            dialect {normalization_dialect},
        );

        SELECT col::text, ds::text
    """
    )
    model = load_sql_based_model(
        expressions, time_column_format="%Y", default_catalog=default_catalog
    )
    assert model.fqn == expected
    # double serialization to ensure even multiple passes don't change results
    model_serialized = SqlModel.parse_raw(SqlModel.parse_raw(model.json()).json())
    assert model_serialized.fqn == expected


def test_model_normalization_quote_flexibility():
    assert (
        normalize_model_name("`catalog`.`db`.`table`", default_catalog=None, dialect="spark")
        == '"catalog"."db"."table"'
    )
    # This takes advantage of the fact that although double quotes ('"') aren't valid quotes in spark, sqlglot still allows it
    assert (
        normalize_model_name('"catalog"."db"."table"', default_catalog=None, dialect="spark")
        == '"catalog"."db"."table"'
    )


def test_macro_parse():
    q = parse_one(
        """select * from table(@get(x) OVER (PARTITION BY y ORDER BY z)) AS results""",
        read="snowflake",
    )
    assert (
        q.sql()
        == "SELECT * FROM TABLE(@get(x) OVER (PARTITION BY y ORDER BY z NULLS LAST)) AS results"
    )


def test_conditional_statement():
    q = parse_one(
        """
        @IF(
          TRUE,
          COPY INTO 's3://example/data.csv'
            FROM EXTRA.EXAMPLE.TABLE
            STORAGE_INTEGRATION = S3_INTEGRATION
            FILE_FORMAT = (TYPE = CSV COMPRESSION = NONE NULL_IF = ('') FIELD_OPTIONALLY_ENCLOSED_BY = '"')
            HEADER = TRUE
            OVERWRITE = TRUE
            SINGLE = TRUE
        ) -- this is a comment
        """,
        read="snowflake",
    )
    assert (
        q.sql("snowflake")
        == "@IF(TRUE, COPY INTO 's3://example/data.csv' FROM EXTRA.EXAMPLE.TABLE STORAGE_INTEGRATION = S3_INTEGRATION FILE_FORMAT = (TYPE=CSV COMPRESSION=NONE NULL_IF=('') FIELD_OPTIONALLY_ENCLOSED_BY='\"') HEADER = TRUE OVERWRITE = TRUE SINGLE = TRUE /* this is a comment */)"
    )

    q = parse_one("@IF(cond, VACUUM ANALYZE);", read="postgres")
    assert q.sql(dialect="postgres") == "@IF(cond, VACUUM ANALYZE)"

    # Verify that the original error case from issue #5823 (Required keyword: 'true' missing) is resolved.
    # It must be parsed as a macro function containing an Anonymous expression rather than exp.If.
    q = parse_one("@IF(1 = 1, ALTER TABLE x ADD y INT);", read="tsql")
    assert q.sql(dialect="tsql") == "@IF(1 = 1, ALTER TABLE x ADD y INTEGER)"
    assert isinstance(q.this, exp.Anonymous)
    assert q.this.name == "IF"

    # Note: SQLGlot's fallback Command parser strips quotes from string literal tokens when parsing unparsed commands
    q = parse_one("@IF(cond, PRINT 'hello');", read="tsql")
    assert q.sql(dialect="tsql") == "@IF(cond, PRINT hello)"

    q = parse_one("@IF(@runtime_stage = 'evaluating', SELECT 1);", read="tsql")
    assert q.sql(dialect="tsql") == "@IF(@runtime_stage = 'evaluating', SELECT 1)"


def test_model_name_cannot_be_string():
    with pytest.raises(ParseError) as parse_error:
        parse(
            """
            MODEL(
              name 'schema.table',
              kind FULL
            );

            SELECT
              1 AS c
            """
        )

    assert "\\'name\\' property cannot be a string value" in str(parse_error)


def test_parse_snowflake_create_schema_ddl():
    assert parse_one("CREATE SCHEMA d.s", dialect="snowflake").sql() == "CREATE SCHEMA d.s"


@pytest.mark.parametrize("dialect", sorted(set(DIALECT_TO_TYPE.values())))
def test_sqlglot_extended_correctly(dialect: str) -> None:
    # MODEL is a SQLMesh extension and not part of SQLGlot
    # If we can roundtrip an expression containing MODEL across every dialect, then the SQLMesh extensions have been registered correctly
    ast = d.parse_one("MODEL (name foo)", dialect=dialect)
    assert isinstance(ast, d.Model)
    name_prop = ast.find(exp.Property)
    assert isinstance(name_prop, exp.Property)
    assert name_prop.this == "name"
    value = name_prop.args["value"]
    assert isinstance(value, exp.Table)
    assert value.sql() == "foo"
    assert ast.sql(dialect=dialect) == "MODEL (\nname foo\n)"


def test_connected_identifier():
    ast = d.parse_one("""SELECT ("x"at time zone 'utc')::timestamp as x""", "redshift")
    assert ast.sql("redshift") == """SELECT CAST(("x" AT TIME ZONE 'utc') AS TIMESTAMP) AS x"""


def test_pipe_syntax():
    ast = d.parse_one("SELECT * FROM (FROM t2 |> SELECT id)", "bigquery")
    assert (
        ast.sql("bigquery")
        == "SELECT * FROM (WITH __tmp1 AS (SELECT id FROM t2) SELECT * FROM __tmp1)"
    )
