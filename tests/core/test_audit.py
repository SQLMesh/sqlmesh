import json
import pytest
from sqlglot import exp, parse_one

from sqlmesh.core import constants as c
from sqlmesh.core.config.model import ModelDefaultsConfig
from sqlmesh.core.context import Context
from sqlmesh.core.node import DbtNodeInfo
from sqlmesh.core.audit import (
    ModelAudit,
    StandaloneAudit,
    builtin,
    load_audit,
    load_multiple_audits,
)
from sqlmesh.core.dialect import parse, jinja_query
from sqlmesh.core.model import (
    FullKind,
    IncrementalByTimeRangeKind,
    Model,
    SeedModel,
    create_sql_model,
    load_sql_based_model,
)
from sqlmesh.utils.errors import AuditConfigError
from sqlmesh.utils.jinja import JinjaMacroRegistry, MacroExtractor
from sqlmesh.utils.metaprogramming import Executable


@pytest.fixture
def model() -> Model:
    return create_sql_model(
        "db.test_model",
        parse_one("SELECT a, b, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
    )


@pytest.fixture
def model_default_catalog() -> Model:
    return create_sql_model(
        "db.test_model",
        parse_one("SELECT a, b, ds"),
        kind=IncrementalByTimeRangeKind(time_column="ds"),
        default_catalog="test_catalog",
    )


def test_load(assert_exp_eq):
    expressions = parse(
        """
        -- Audit comment
        Audit (
            name my_audit,
            dialect spark,
            blocking false,
        );

        SELECT
            *
        FROM
            db.table
        WHERE
            col IS NULL
    """
    )

    audit = load_audit(expressions, path="/path/to/audit", dialect="duckdb")
    assert isinstance(audit, ModelAudit)
    assert audit.dialect == "spark"
    assert audit.blocking is False
    assert audit.skip is False
    assert_exp_eq(
        audit.query,
        """
    SELECT
        *
    FROM
        db.table
    WHERE
        col IS NULL
    """,
    )
    assert audit.query_._parsed is not None
    assert audit.query_._parsed_dialect == "spark"


def test_load_standalone(assert_exp_eq):
    expressions = parse(
        """
        Audit (
            name my_audit,
            dialect spark,
            blocking false,
            standalone true,
            cron '@hourly',
            owner 'Sally',
        );

        SELECT
            *
        FROM
            db.table
        WHERE
            col IS NULL
    """
    )

    audit = load_audit(expressions, path="/path/to/audit", dialect="duckdb")
    assert isinstance(audit, StandaloneAudit)
    assert audit.dialect == "spark"
    assert audit.blocking is False
    assert audit.skip is False
    assert audit.cron == "@hourly"
    assert audit.owner == "Sally"
    assert_exp_eq(
        audit.query,
        """
    SELECT
        *
    FROM
        db.table
    WHERE
        col IS NULL
    """,
    )
    assert audit.query_._parsed is not None
    assert audit.query_._parsed_dialect == "spark"


def test_load_standalone_default_catalog(assert_exp_eq):
    expressions = parse(
        """
        Audit (
            name my_audit,
            dialect spark,
            blocking false,
            standalone true,
            cron '@hourly',
            owner 'Sally',
        );

        SELECT
            *
        FROM
            db.table
        WHERE
            col IS NULL
    """
    )

    audit = load_audit(
        expressions, path="/path/to/audit", dialect="duckdb", default_catalog="test_catalog"
    )
    assert isinstance(audit, StandaloneAudit)
    assert audit.dialect == "spark"
    assert audit.blocking is False
    assert audit.skip is False
    assert audit.cron == "@hourly"
    assert audit.owner == "Sally"
    assert audit.default_catalog == "test_catalog"
    assert audit.name == "my_audit"
    assert audit.fqn == "my_audit"
    assert_exp_eq(
        audit.query,
        """
    SELECT
        *
    FROM
        db.table
    WHERE
        col IS NULL
    """,
    )
    assert_exp_eq(
        audit.render_audit_query(),
        """
    SELECT
        *
    FROM
        "test_catalog"."db"."table" AS "table"
    WHERE
        "col" IS NULL
    """,
    )


def test_load_standalone_with_macros(assert_exp_eq):
    expressions = parse(
        """
        AUDIT (
            name my_audit,
            owner owner_name,
            dialect spark,
            standalone true,
        );

        @DEF(x, 1);
        CACHE TABLE x AS SELECT 1;
        ADD JAR 's3://my_jar.jar';

        SELECT
            *,
            @test_macro(1),
        FROM
            db.table t1
        WHERE
            col IS NULL
    """
    )

    audit = load_audit(
        expressions,
        macros={
            "test_macro": Executable(payload="def test_macro(evaluator, v):\n    return v"),
            "extra_macro": Executable(payload="def extra_macro(evaluator, v):\n    return v + 1"),
        },
    )

    assert "test_macro" in audit.python_env
    assert "extra_macro" not in audit.python_env


def test_load_standalone_with_jinja_macros(assert_exp_eq):
    expressions = parse(
        """
        AUDIT (
            name my_audit,
            owner owner_name,
            dialect spark,
            standalone true,
        );

        JINJA_QUERY_BEGIN;
        SELECT
            *,
            {{ test_macro(1) }},
        FROM
            db.table t1
        WHERE
            col IS NULL
        JINJA_QUERY_END;
    """
    )

    macros = """
    {% macro test_macro(v) %}{{ v }}{% endmacro %}

    {% macro extra_macro(v) %}{{ v + 1 }}{% endmacro %}
    """

    jinja_macros = JinjaMacroRegistry()
    jinja_macros.add_macros(MacroExtractor().extract(macros))
    audit = load_audit(
        expressions,
        jinja_macros=jinja_macros,
    )

    assert "test_macro" in audit.jinja_macros.root_macros
    assert "extra_macro" not in audit.jinja_macros.root_macros


def test_load_multiple(assert_exp_eq):
    expressions = parse(
        """
        Audit (
            name first_audit,
            dialect spark,
        );

        SELECT *
        FROM db.table
        WHERE col1 IS NULL;

        Audit (
            name second_audit,
            dialect duckdb,
            blocking false,
        );

        SELECT *
        FROM db.table
        WHERE col2 IS NULL;
    """
    )

    first_audit, second_audit = load_multiple_audits(expressions, path="/path/to/audit")
    assert first_audit.dialect == "spark"
    assert first_audit.blocking is True
    assert first_audit.skip is False
    assert_exp_eq(
        first_audit.query,
        """
    SELECT *
    FROM db.table
    WHERE col1 IS NULL
    """,
    )

    assert second_audit.dialect == "duckdb"
    assert second_audit.blocking is False
    assert second_audit.skip is False
    assert_exp_eq(
        second_audit.query,
        """
    SELECT *
    FROM db.table
    WHERE col2 IS NULL
    """,
    )


def test_load_with_dictionary_defaults():
    expressions = parse(
        """
        AUDIT (
            name my_audit,
            dialect spark,
            defaults (
                field1 = some_column,
                field2 = 3
            ),
        );

        SELECT 1
    """
    )

    audit = load_audit(expressions, dialect="spark")
    assert audit.defaults.keys() == {"field1", "field2"}
    for value in audit.defaults.values():
        assert isinstance(value, exp.Expr)


def test_load_with_single_defaults():
    # testing it also works with a single default with no trailing comma
    expressions = parse(
        """
        AUDIT (
            name my_audit,
            defaults (
                field1 = some_column
            ),
        );

        SELECT 1
    """
    )

    audit = load_audit(expressions, dialect="duckdb")
    assert audit.defaults.keys() == {"field1"}
    for value in audit.defaults.values():
        assert isinstance(value, exp.Expr)


def test_no_audit_statement():
    expressions = parse(
        """
        SELECT 1
    """
    )
    with pytest.raises(AuditConfigError) as ex:
        load_audit(expressions, path="/path/to/audit", dialect="duckdb")
    assert "Incomplete audit definition" in str(ex.value)


def test_unordered_audit_statements():
    expressions = parse(
        """
        SELECT 1;

        AUDIT (
            name my_audit,
        );
    """
    )

    with pytest.raises(AuditConfigError) as ex:
        load_audit(expressions, path="/path/to/audit", dialect="duckdb")
    assert "AUDIT statement is required as the first statement" in str(ex.value)


def test_no_query():
    expressions = parse(
        """
        AUDIT (
            name my_audit,
        );

        @DEF(x, 1)
    """
    )

    with pytest.raises(AuditConfigError) as ex:
        load_audit(expressions, path="/path/to/audit", dialect="duckdb")
    assert "Missing SELECT query" in str(ex.value)


def test_macro(model: Model):
    expected_query = """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE "a" IS NULL"""

    audit = ModelAudit(
        name="test_audit",
        query="SELECT * FROM @this_model WHERE a IS NULL",
    )

    audit_jinja = ModelAudit(
        name="test_audit",
        query="JINJA_QUERY_BEGIN; SELECT * FROM {{ this_model }} WHERE a IS NULL; JINJA_END;",
    )

    assert model.render_audit_query(audit).sql() == expected_query
    assert model.render_audit_query(audit_jinja).sql() == expected_query


def test_load_with_defaults(model, assert_exp_eq):
    expressions = parse(
        """
        Audit (
            name my_audit,
            defaults (
                field1 = some_column,
                field2 = 3,
                field3 = other_column,
                field4 = 'some string'
            )
        );

        SELECT
            *
        FROM
            db.table
        WHERE True
            AND @IF(@field4 = 'overridden', @field4 IN ('some string', 'other string'), 1=1)
            AND @field1 = @field2
            AND @field3 != @field4
    """
    )
    audit = load_audit(expressions, path="/path/to/audit", dialect="duckdb")
    assert audit.defaults == {
        "field1": exp.to_column("some_column"),
        "field2": exp.Literal.number(3),
        "field3": exp.to_column("other_column"),
        "field4": exp.Literal.string("some string"),
    }
    assert_exp_eq(
        model.render_audit_query(audit, field4=exp.Literal.string("overridden")),
        'SELECT * FROM "db"."table" AS "table" WHERE TRUE AND \'overridden\' IN (\'some string\', \'other string\') AND "some_column" = 3 AND "other_column" <> \'overridden\'',
    )


def test_not_null_audit(model: Model):
    rendered_query_a = model.render_audit_query(
        builtin.not_null_audit,
        columns=[exp.to_column("a")],
    )
    assert (
        rendered_query_a.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE "a" IS NULL AND TRUE"""
    )

    rendered_query_a_and_b = model.render_audit_query(
        builtin.not_null_audit,
        columns=[exp.to_column("a"), exp.to_column("b")],
    )
    assert (
        rendered_query_a_and_b.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE ("a" IS NULL OR "b" IS NULL) AND TRUE"""
    )


def test_not_null_audit_default_catalog(model_default_catalog: Model):
    rendered_query_a = model_default_catalog.render_audit_query(
        builtin.not_null_audit,
        columns=[exp.to_column("a")],
    )
    assert (
        rendered_query_a.sql()
        == """SELECT * FROM (SELECT * FROM "test_catalog"."db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE "a" IS NULL AND TRUE"""
    )

    rendered_query_a_and_b = model_default_catalog.render_audit_query(
        builtin.not_null_audit,
        columns=[exp.to_column("a"), exp.to_column("b")],
    )
    assert (
        rendered_query_a_and_b.sql()
        == """SELECT * FROM (SELECT * FROM "test_catalog"."db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE ("a" IS NULL OR "b" IS NULL) AND TRUE"""
    )


def test_unique_values_audit(model: Model):
    rendered_query_a = model.render_audit_query(
        builtin.unique_values_audit, columns=[exp.to_column("a")], condition=parse_one("b IS NULL")
    )
    assert (
        rendered_query_a.sql()
        == 'SELECT * FROM (SELECT ROW_NUMBER() OVER (PARTITION BY "a" ORDER BY "a") AS "rank_a" FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE "b" IS NULL) AS "_1" WHERE "rank_a" > 1'
    )

    rendered_query_a_and_b = model.render_audit_query(
        builtin.unique_values_audit, columns=[exp.to_column("a"), exp.to_column("b")]
    )
    assert (
        rendered_query_a_and_b.sql()
        == 'SELECT * FROM (SELECT ROW_NUMBER() OVER (PARTITION BY "a" ORDER BY "a") AS "rank_a", ROW_NUMBER() OVER (PARTITION BY "b" ORDER BY "b") AS "rank_b" FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE TRUE) AS "_1" WHERE "rank_a" > 1 OR "rank_b" > 1'
    )


def test_accepted_values_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.accepted_values_audit,
        column=exp.to_column("a"),
        is_in=["value_a", "value_b"],
    )
    assert (
        rendered_query.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE NOT "a" IN ('value_a', 'value_b') AND TRUE"""
    )


def test_number_of_rows_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.number_of_rows_audit,
        threshold=0,
    )
    assert (
        rendered_query.sql()
        == """SELECT COUNT(*) FROM (SELECT 1 FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE TRUE LIMIT 0 + 1) AS "_1" HAVING COUNT(*) <= 0"""
    )


def test_forall_audit(model: Model):
    rendered_query_a = model.render_audit_query(
        builtin.forall_audit,
        criteria=[parse_one("a >= b")],
    )
    assert (
        rendered_query_a.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE NOT ("a" >= "b") AND TRUE"""
    )

    rendered_query_a = model.render_audit_query(
        builtin.forall_audit,
        criteria=[parse_one("a >= b"), parse_one("c + d - e < 1.0")],
    )
    assert (
        rendered_query_a.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE (NOT ("a" >= "b") OR NOT ("c" + "d" - "e" < 1.0)) AND TRUE"""
    )

    rendered_query_a = model.render_audit_query(
        builtin.forall_audit,
        criteria=[parse_one("a >= b"), parse_one("c + d - e < 1.0")],
        condition=parse_one("f = 42"),
    )
    assert (
        rendered_query_a.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE (NOT ("a" >= "b") OR NOT ("c" + "d" - "e" < 1.0)) AND "f" = 42"""
    )


def test_accepted_range_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.accepted_range_audit, column=exp.to_column("a"), min_v=0
    )
    assert (
        rendered_query.sql()
        == 'SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE "a" < 0 AND TRUE'
    )
    rendered_query = model.render_audit_query(
        builtin.accepted_range_audit, column=exp.to_column("a"), max_v=100, inclusive=exp.false()
    )
    assert (
        rendered_query.sql()
        == 'SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE "a" >= 100 AND TRUE'
    )
    rendered_query = model.render_audit_query(
        builtin.accepted_range_audit, column=exp.to_column("a"), min_v=100, max_v=100
    )
    assert (
        rendered_query.sql()
        == 'SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE ("a" < 100 OR "a" > 100) AND TRUE'
    )


def test_at_least_one_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.at_least_one_audit,
        column=exp.to_column("a"),
    )
    assert (
        rendered_query.sql()
        == 'SELECT 1 AS "1" FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE TRUE GROUP BY 1 HAVING COUNT("a") = 0'
    )


def test_mutually_exclusive_ranges_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.mutually_exclusive_ranges_audit,
        lower_bound_column=exp.to_column("a"),
        upper_bound_column=exp.to_column("a"),
    )
    assert (
        rendered_query.sql()
        == '''WITH "window_functions" AS (SELECT "a" AS "lower_bound", "a" AS "upper_bound", LEAD("a") OVER (ORDER BY "a", "a") AS "next_lower_bound", ROW_NUMBER() OVER (ORDER BY "a" DESC, "a" DESC) = 1 AS "is_last_record" FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE TRUE), "calc" AS (SELECT *, COALESCE("lower_bound" <= "upper_bound", FALSE) AS "lower_bound_lte_upper_bound", COALESCE("upper_bound" <= "next_lower_bound", "is_last_record", FALSE) AS "upper_bound_lte_next_lower_bound" FROM "window_functions" AS "window_functions"), "validation_errors" AS (SELECT * FROM "calc" AS "calc" WHERE NOT ("lower_bound_lte_upper_bound" AND "upper_bound_lte_next_lower_bound")) SELECT * FROM "validation_errors" AS "validation_errors"'''
    )


def test_sequential_values_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.sequential_values_audit,
        column=exp.to_column("a"),
    )
    assert (
        rendered_query.sql()
        == '''WITH "windowed" AS (SELECT "a", LAG("a") OVER (ORDER BY "a") AS "prv" FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE TRUE), "validation_errors" AS (SELECT * FROM "windowed" AS "windowed" WHERE NOT ("a" = "prv" + 1)) SELECT * FROM "validation_errors" AS "validation_errors"'''
    )


def test_chi_square_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.chi_square_audit,
        column_a=exp.to_column("a"),
        column_b=exp.to_column("b"),
        critical_value=exp.convert(9.48773),
    )
    assert (
        rendered_query.sql()
        == """WITH "samples" AS (SELECT "a" AS "x_a", "b" AS "x_b" FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE (NOT "a" IS NULL AND NOT "b" IS NULL) AND TRUE), "contingency_table" AS (SELECT "x_a", "x_b", COUNT(*) AS "observed", (SELECT COUNT(*) FROM "samples" AS "t" WHERE "r"."x_a" = "t"."x_a") AS "tot_a", (SELECT COUNT(*) FROM "samples" AS "t" WHERE "r"."x_b" = "t"."x_b") AS "tot_b", (SELECT COUNT(*) FROM "samples" AS "samples") AS "g_t" /* g_t is the grand total */ FROM "samples" AS "r" GROUP BY "x_a", "x_b") SELECT ((SELECT COUNT(DISTINCT "x_a") FROM "contingency_table" AS "contingency_table") - 1) * ((SELECT COUNT(DISTINCT "x_b") FROM "contingency_table" AS "contingency_table") - 1) AS "degrees_of_freedom", SUM(("observed" - ("tot_a" * "tot_b" / "g_t")) * ("observed" - ("tot_a" * "tot_b" / "g_t")) / ("tot_a" * "tot_b" / "g_t")) AS "chi_square" FROM "contingency_table" AS "contingency_table" /* H0: the two variables are independent */ /* H1: the two variables are dependent */ /* if chi_square > critical_value, reject H0 */ /* if chi_square <= critical_value, fail to reject H0 */ HAVING NOT "chi_square" > 9.48773"""
    )


def test_pattern_audits(model: Model):
    rendered_query = model.render_audit_query(
        builtin.match_regex_pattern_list_audit,
        column=exp.to_column("a"),
        patterns=[r"^\d.*", ".*!$"],
    )
    assert (
        rendered_query.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE (NOT REGEXP_LIKE("a", \'^\\d.*\') AND NOT REGEXP_LIKE("a", \'.*!$\')) AND TRUE"""
    )

    rendered_query = model.render_audit_query(
        builtin.not_match_regex_pattern_list_audit,
        column=exp.to_column("a"),
        patterns=[r"^\d.*", ".*!$"],
    )
    assert (
        rendered_query.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE (REGEXP_LIKE("a", \'^\\d.*\') OR REGEXP_LIKE("a", \'.*!$\')) AND TRUE"""
    )

    rendered_query = model.render_audit_query(
        builtin.match_like_pattern_list,
        column=exp.to_column("a"),
        patterns=["jim%", "pam%"],
    )
    assert (
        rendered_query.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE (NOT "a" LIKE \'jim%\' AND NOT "a" LIKE \'pam%\') AND TRUE"""
    )

    rendered_query = model.render_audit_query(
        builtin.not_match_like_pattern_list_audit,
        column=exp.to_column("a"),
        patterns=["jim%", "pam%"],
    )
    assert (
        rendered_query.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN \'1970-01-01\' AND \'1970-01-01\') AS "_0" WHERE ("a" LIKE \'jim%\' OR "a" LIKE \'pam%\') AND TRUE"""
    )


def test_standalone_audit(model: Model, assert_exp_eq):
    audit = StandaloneAudit(
        name="test_audit", query=parse_one(f"SELECT * FROM {model.name} WHERE col IS NULL")
    )

    assert audit.depends_on == {model.fqn}

    rendered_query = audit.render_audit_query()
    assert_exp_eq(
        rendered_query, """SELECT * FROM "db"."test_model" AS "test_model" WHERE "col" IS NULL"""
    )

    with pytest.raises(AuditConfigError) as ex:
        StandaloneAudit(name="test_audit", query=parse_one("SELECT 1"), blocking=True)

    assert "Standalone audits cannot be blocking: 'test_audit'." in str(ex.value)


def test_render_definition():
    expressions = parse(
        """
        AUDIT (
            name my_audit,
            dialect spark,
            owner owner_name,
            standalone true,
        );

        @DEF(x, 1);
        CACHE TABLE x AS SELECT 1;
        ADD JAR 's3://my_jar.jar';

        SELECT
            *,
            @test_macro(1),
        FROM
            db.table t1
        WHERE
            col IS NULL
    """
    )

    audit = load_audit(
        expressions,
        macros={"test_macro": Executable(payload="def test_macro(evaluator, v):\n    return v")},
    )

    from sqlmesh.core.dialect import format_model_expressions

    # Should not include the macro implementation.
    assert format_model_expressions(
        audit.render_definition(include_python=False)
    ) == format_model_expressions(expressions)

    # Should include the macro implementation.
    assert "def test_macro(evaluator, v):" in format_model_expressions(audit.render_definition())


def test_render_definition_dbt_node_info():
    node_info = DbtNodeInfo(
        unique_id="test.project.my_audit", name="my_audit", fqn="project.my_audit"
    )

    audit = StandaloneAudit(name="my_audit", dbt_node_info=node_info, query=jinja_query("select 1"))

    assert (
        audit.render_definition()[0].sql(pretty=True)
        == """AUDIT (
  name my_audit,
  dbt_node_info (
    fqn := 'project.my_audit',
    name := 'my_audit',
    unique_id := 'test.project.my_audit'
  ),
  standalone TRUE
)"""
    )


def test_text_diff():
    expressions = parse(
        """
        AUDIT (
            name my_audit,
            dialect spark,
            owner owner_name,
            standalone true,
        );

        SELECT
            *,
            @test_macro(1),
        FROM
            db.table t1
        WHERE
            col IS NULL
    """
    )

    audit = load_audit(
        expressions,
        macros={"test_macro": Executable(payload="def test_macro(evaluator, v):\n    return v")},
    )

    modified_audit = audit.copy()
    modified_audit.name = "my_audit_2"

    assert (
        "\n".join(l.rstrip() for l in audit.text_diff(modified_audit).split("\n"))
        == """---

+++

@@ -1,5 +1,5 @@

 AUDIT (
-  name my_audit,
+  name my_audit_2,
   dialect spark,
   owner owner_name,
   standalone TRUE"""
    )


def test_non_blocking_builtin():
    from sqlmesh.core.audit.builtin import BUILT_IN_AUDITS

    assert BUILT_IN_AUDITS["not_null_non_blocking"].blocking is False
    assert BUILT_IN_AUDITS["not_null_non_blocking"].name == "not_null_non_blocking"
    assert BUILT_IN_AUDITS["not_null"].query == BUILT_IN_AUDITS["not_null_non_blocking"].query


def test_string_length_between_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.string_length_between_audit,
        column=exp.column("x"),
        min_v=1,
        max_v=5,
    )
    assert (
        rendered_query.sql()
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE (LENGTH("x") < 1 OR LENGTH("x") > 5) AND TRUE"""
    )


def test_not_constant_audit(model: Model):
    rendered_query = model.render_audit_query(
        builtin.not_constant_audit, column=exp.column("x"), condition=exp.condition("x > 1")
    )
    assert (
        rendered_query.sql()
        == """SELECT 1 AS "1" FROM (SELECT COUNT(DISTINCT "x") AS "t_cardinality" FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE "x" > 1) AS "r" WHERE "r"."t_cardinality" <= 1"""
    )


def test_condition_with_macro_var(model: Model):
    rendered_query = model.render_audit_query(
        builtin.not_null_audit,
        columns=[exp.column("x")],
        condition=exp.condition("dt BETWEEN @start_dt AND @end_dt"),
    )
    assert (
        rendered_query.sql(dialect="duckdb")
        == """SELECT * FROM (SELECT * FROM "db"."test_model" AS "test_model" WHERE "ds" BETWEEN '1970-01-01' AND '1970-01-01') AS "_0" WHERE "x" IS NULL AND "dt" BETWEEN CAST('1970-01-01 00:00:00+00:00' AS TIMESTAMPTZ) AND CAST('1970-01-01 23:59:59.999999+00:00' AS TIMESTAMPTZ)"""
    )


def test_variables(assert_exp_eq):
    expressions = parse(
        """
        Audit (
            name my_audit,
            dialect bigquery,
            standalone true,
        );

        SELECT
            *
        FROM
            db.table
        WHERE
            col = @VAR('test_var')
    """
    )

    audit = load_audit(
        expressions,
        path="/path/to/audit",
        dialect="bigquery",
        variables={"test_var": "test_val", "test_var_unused": "unused_val"},
    )
    assert audit.python_env[c.SQLMESH_VARS] == Executable.value({"test_var": "test_val"})
    assert (
        audit.render_audit_query().sql(dialect="bigquery")
        == "SELECT * FROM `db`.`table` AS `table` WHERE `col` = 'test_val'"
    )


def test_load_inline_audits(assert_exp_eq):
    expressions = parse(
        """
        MODEL (
            name db.table,
            dialect spark,
            audits(does_not_exceed_threshold, assert_positive_id)
        );

        SELECT id FROM tbl;

        AUDIT (
        name does_not_exceed_threshold,
        );
        SELECT * FROM @this_model
        WHERE @column >= @threshold;

        AUDIT (
        name assert_positive_id,
        );
        SELECT *
        FROM @this_model
        WHERE
        id < 0;
    """
    )

    model = load_sql_based_model(expressions)
    assert len(model.audits) == 2
    assert len(model.audits_with_args) == 2
    assert isinstance(model.audit_definitions["assert_positive_id"], ModelAudit)
    assert isinstance(model.audit_definitions["does_not_exceed_threshold"], ModelAudit)


def test_model_inline_audits(sushi_context: Context):
    model_name = "sushi.waiter_names"
    expected_query = 'SELECT * FROM (SELECT * FROM "memory"."sushi"."waiter_names" AS "waiter_names") AS "_0" WHERE "id" < 0'
    model = sushi_context.get_snapshot(model_name, raise_if_missing=True).node

    assert isinstance(model, SeedModel)
    assert len(model.audit_definitions) == 3
    assert isinstance(model.audit_definitions["assert_valid_name"], ModelAudit)
    model.render_audit_query(model.audit_definitions["assert_positive_id"]).sql() == expected_query


def test_audit_query_normalization():
    model = create_sql_model(
        "db.test_model",
        parse_one("SELECT a, b, ds"),
        kind=FullKind(),
        dialect="snowflake",
    )
    rendered_audit_query = model.render_audit_query(
        builtin.not_null_audit,
        columns=[exp.to_column("a")],
    )
    assert (
        rendered_audit_query.sql("snowflake")
        == """SELECT * FROM "DB"."TEST_MODEL" AS "TEST_MODEL" WHERE "A" IS NULL AND TRUE"""
    )


def test_rendered_diff():
    audit1 = StandaloneAudit(
        name="test_audit", query=parse_one("SELECT * FROM 'test' WHERE @AND(TRUE, NULL) > 2")
    )

    audit2 = StandaloneAudit(
        name="test_audit", query=parse_one("SELECT * FROM 'test' WHERE @OR(FALSE, NULL) > 2")
    )

    assert """@@ -6,4 +6,4 @@

   *
 FROM "test" AS "test"
 WHERE
-  TRUE > 2
+  FALSE > 2""" in audit1.text_diff(audit2, rendered=True)


def test_multiple_audits_with_same_name():
    expressions = parse(
        """
        MODEL (
            name db.table,
            dialect spark,
            audits(
                does_not_exceed_threshold(column := id, threshold := 1000),
                does_not_exceed_threshold(column := price, threshold := 100),
                does_not_exceed_threshold(column := price, threshold := 100)
            )
        );

        SELECT id, price FROM tbl;

        AUDIT (
            name does_not_exceed_threshold,
        );
        SELECT * FROM @this_model
        WHERE @column >= @threshold;
        """
    )
    model = load_sql_based_model(expressions)
    assert len(model.audits) == 3
    assert len(model.audits_with_args) == 3
    assert len(model.audit_definitions) == 1

    # Testing that audit names are identical
    assert model.audits[0][0] == model.audits[1][0] == model.audits[2][0]

    # Testing that audit arguments are different for first and second audit
    assert model.audits[0][1] != model.audits[1][1]

    # Testing that audit arguments are identical for second and third audit
    # This establishes that identical audits are preserved
    assert model.audits[1][1] == model.audits[2][1]


def test_default_audits_included_when_no_model_audits():
    expressions = parse("""
    MODEL (
        name test.basic_model
    );
    SELECT 1 as id, 'test' as name;
    """)

    model_defaults = ModelDefaultsConfig(
        dialect="duckdb", audits=["not_null(columns := ['id'])", "unique_values(columns := ['id'])"]
    )
    model = load_sql_based_model(expressions, defaults=model_defaults.dict())

    assert len(model.audits) == 2
    audit_names = [audit[0] for audit in model.audits]
    assert "not_null" in audit_names
    assert "unique_values" in audit_names

    # Verify arguments are preserved
    for audit_name, audit_args in model.audits:
        if audit_name == "not_null":
            assert "columns" in audit_args
            assert audit_args["columns"].expressions[0].this == "id"
        elif audit_name == "unique_values":
            assert "columns" in audit_args
            assert audit_args["columns"].expressions[0].this == "id"

    for audit_name, audit_args in model.audits_with_args:
        if audit_name == "not_null":
            assert "columns" in audit_args
            assert audit_args["columns"].expressions[0].this == "id"
        elif audit_name == "unique_values":
            assert "columns" in audit_args
            assert audit_args["columns"].expressions[0].this == "id"


def test_model_defaults_audits_with_same_name():
    expressions = parse(
        """
        MODEL (
            name db.table,
            dialect spark,
            audits(
                does_not_exceed_threshold(column := id, threshold := 1000),
                does_not_exceed_threshold(column := price, threshold := 100),
                unique_values(columns := ['id'])
            )
        );

        SELECT id, price FROM tbl;

        AUDIT (
            name does_not_exceed_threshold,
        );
        SELECT * FROM @this_model
        WHERE @column >= @threshold;
        """
    )

    model_defaults = ModelDefaultsConfig(
        dialect="duckdb",
        audits=[
            "does_not_exceed_threshold(column := price, threshold := 33)",
            "does_not_exceed_threshold(column := id, threshold := 65)",
            "not_null(columns := ['id'])",
        ],
    )
    model = load_sql_based_model(expressions, defaults=model_defaults.dict())
    assert len(model.audits) == 6
    assert len(model.audits_with_args) == 6
    assert len(model.audit_definitions) == 1

    expected_audits = [
        (
            "does_not_exceed_threshold",
            {"column": exp.column("price"), "threshold": exp.Literal.number(33)},
        ),
        (
            "does_not_exceed_threshold",
            {"column": exp.column("id"), "threshold": exp.Literal.number(65)},
        ),
        ("not_null", {"columns": exp.convert(["id"])}),
        (
            "does_not_exceed_threshold",
            {"column": exp.column("id"), "threshold": exp.Literal.number(1000)},
        ),
        (
            "does_not_exceed_threshold",
            {"column": exp.column("price"), "threshold": exp.Literal.number(100)},
        ),
        ("unique_values", {"columns": exp.convert(["id"])}),
    ]

    for (actual_name, actual_args), (expected_name, expected_args) in zip(
        model.audits, expected_audits
    ):
        # Validate the audit names are preserved
        assert actual_name == expected_name
        for key in expected_args:
            # comparing sql representaion is easier
            assert actual_args[key].sql() == expected_args[key].sql()

    # Validate audits with args as well along with their arguments
    for (actual_audit, actual_args), (expected_name, expected_args) in zip(
        model.audits_with_args, expected_audits
    ):
        assert actual_audit.name == expected_name
        for key in expected_args:
            assert actual_args[key].sql() == expected_args[key].sql()


def test_audit_formatting_flag_serde():
    expressions = parse(
        """
        AUDIT (
            name my_audit,
            dialect bigquery,
            formatting false,
        );

        SELECT * FROM db.table WHERE col = @VAR('test_var')
    """
    )

    audit = load_audit(
        expressions,
        path="/path/to/audit",
        dialect="bigquery",
        variables={"test_var": "test_val", "test_var_unused": "unused_val"},
    )

    audit_json = audit.json()

    assert "formatting" not in json.loads(audit_json)

    deserialized_audit = ModelAudit.parse_raw(audit_json)
    assert deserialized_audit.dict() == audit.dict()
