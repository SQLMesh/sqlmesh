import pytest
from sqlglot.optimizer.qualify import qualify

from sqlmesh.core import dialect as d
from sqlmesh.core.metric import expand_metrics, load_metric_ddl, rewrite
from sqlmesh.core.metric.rewriter import Rewriter
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.reference import ReferenceGraph
from sqlmesh.utils import UniqueKeyDict
from sqlmesh.utils.errors import ConfigError, SQLMeshError


def _model(name, columns, grain, references=""):
    return load_sql_based_model(
        d.parse(
            f"""
            MODEL (
                name {name},
                dialect duckdb,
                columns ({columns}),
                grain {grain}
                {f", references ({references})" if references else ""}
            );
            SELECT {", ".join(column.split()[0] for column in columns.split(","))}
            FROM raw.source
            """
        )
    )


@pytest.fixture
def metrics_runtime():
    import duckdb

    columns = "org VARCHAR, channel VARCHAR, amount INT, status VARCHAR"
    models = [
        _model(f"facts.{name}", f"{name}_id INT, {columns}", f"{name}_id", "org")
        for name in ("a", "b", "c")
    ]
    models.append(
        _model("dims.organizations", "org VARCHAR, region VARCHAR, status VARCHAR", "org")
    )
    graph = ReferenceGraph(models)
    metas = UniqueKeyDict("metrics")
    for expression in d.parse(
        """
        METRIC(name a_sum, expression SUM(facts.a.amount));
        METRIC(name b_sum, expression SUM(facts.b.amount));
        METRIC(name c_sum, expression SUM(facts.c.amount));
        METRIC(name ratio, expression a_sum / b_sum);
        """
    ):
        meta = load_metric_ddl(expression, dialect="duckdb")
        metas[meta.name] = meta
    metrics = expand_metrics(metas)
    with duckdb.connect(":memory:") as connection:
        connection.execute("CREATE SCHEMA facts; CREATE SCHEMA dims")
        for name in ("a", "b", "c"):
            connection.execute(f"CREATE TABLE facts.{name} ({name}_id INT, {columns})")
        connection.execute(
            """
            INSERT INTO facts.a VALUES
                (1, 'A', 'web', 10, 'fact'),
                (2, 'A', 'app', 20, 'fact'),
                (3, 'B', 'web', 100, 'fact');
            INSERT INTO facts.b VALUES
                (11, 'A', 'web', 2, 'fact'),
                (12, 'A', 'app', 3, 'fact'),
                (13, 'A', 'app', 5, 'fact'),
                (14, 'B', 'web', 50, 'fact');
            INSERT INTO facts.c VALUES
                (21, 'B', 'web', 4, 'fact'),
                (22, 'C', 'app', 7, 'fact'),
                (23, 'D', 'web', 8, 'fact');
            CREATE TABLE dims.organizations (org VARCHAR, region VARCHAR, status VARCHAR);
            INSERT INTO dims.organizations VALUES
                ('A', 'north', 'ACTIVE'),
                ('B', 'south', 'INACTIVE'),
                ('C', 'north', 'ACTIVE'),
                ('D', 'south', 'INACTIVE');
            """
        )
        yield connection, graph, metrics


def _execute(runtime, sql, join_type=None):
    connection, graph, metrics = runtime
    if join_type is None:
        query = rewrite(sql, graph=graph, metrics=metrics, dialect="duckdb")
    else:
        query = Rewriter(
            graph=graph, metrics=metrics, dialect="duckdb", join_type=join_type
        ).rewrite(qualify(d.parse_one(sql, dialect="duckdb"), dialect="duckdb"))
    return connection.execute(query.sql("duckdb")).fetchall()


def test_rewrite_single_source(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(a_sum)
        FROM __semantic.__table s
        WHERE s.org = 'A'
        GROUP BY s.org
        """,
    ) == [("A", 30)]


def test_rewrite_physical_base(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT o.org, METRIC(a_sum)
        FROM dims.organizations o
        GROUP BY o.org
        ORDER BY o.org
        """,
    ) == [("A", 30), ("B", 100), ("C", None), ("D", None)]


@pytest.mark.parametrize("grouped", [False, True])
def test_rewrite_shared_filter(metrics_runtime, grouped):
    query = (
        "SELECT s.org, METRIC(a_sum), METRIC(b_sum), METRIC(ratio) "
        if grouped
        else "SELECT METRIC(a_sum), METRIC(b_sum), METRIC(ratio) "
    )
    query += "FROM __semantic.__table s WHERE s.org = 'A'"
    if grouped:
        query += " GROUP BY s.org"
    assert _execute(metrics_runtime, query) == (
        [("A", 30, 10, 3.0)] if grouped else [(30, 10, 3.0)]
    )


def test_rewrite_filter_not_in_grain(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(a_sum), METRIC(b_sum), METRIC(ratio)
        FROM __semantic.__table s
        WHERE s.channel = 'web'
        GROUP BY s.org
        ORDER BY s.org
        """,
    ) == [("A", 10, 2, 5.0), ("B", 100, 50, 2.0)]


def test_rewrite_boolean_filter(metrics_runtime):
    connection, _, _ = metrics_runtime
    connection.execute(
        """
        INSERT INTO facts.a VALUES (4, 'B', 'app', 21, 'fact');
        INSERT INTO facts.b VALUES (15, 'B', 'app', 7, 'fact');
        """
    )
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(a_sum), METRIC(b_sum)
        FROM __semantic.__table s
        WHERE (s.org = 'A' AND s.channel = 'web')
           OR (s.org = 'B' AND s.channel = 'app')
        GROUP BY s.org
        ORDER BY s.org
        """,
    ) == [("A", 10, 2), ("B", 21, 7)]


def test_rewrite_related_dimension_filter(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(a_sum), METRIC(b_sum), METRIC(ratio)
        FROM __semantic.__table s
        WHERE s.region = 'north'
        GROUP BY s.org
        """,
    ) == [("A", 30, 10, 3.0)]


def test_rewrite_dimension_with_multiple_references(metrics_runtime):
    connection, graph, _ = metrics_runtime
    graph.add_model(
        _model(
            "dims.labels",
            "label_id INT, org VARCHAR, label VARCHAR",
            "label_id",
            "org",
        )
    )
    connection.execute(
        """
        CREATE TABLE dims.labels (label_id INT, org VARCHAR, label VARCHAR);
        INSERT INTO dims.labels VALUES (101, 'A', 'included'), (102, 'B', 'excluded');
        """
    )
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(a_sum), METRIC(b_sum)
        FROM __semantic.__table s
        WHERE s.label = 'included'
        GROUP BY s.org
        """,
    ) == [("A", 30, 10)]


def test_rewrite_explicit_dimension_filter(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(a_sum), METRIC(b_sum)
        FROM __semantic.__table s
        LEFT JOIN dims.organizations o ON s.org = o.org
        WHERE o.status = 'ACTIVE'
        GROUP BY s.org
        """,
    ) == [("A", 30, 10)]


@pytest.mark.parametrize("predicate", ["s.unknown = 1", "missing.region = 'north'"])
def test_rewrite_unknown_filter(metrics_runtime, predicate):
    with pytest.raises((ConfigError, SQLMeshError)):
        _execute(
            metrics_runtime,
            f"SELECT METRIC(ratio) FROM __semantic.__table s WHERE {predicate}",
        )


def test_rewrite_ambiguous_filter(metrics_runtime):
    _, graph, _ = metrics_runtime
    graph.add_model(_model("dims.audience", "org VARCHAR, region VARCHAR", "org"))
    with pytest.raises(ConfigError, match="(?i)ambiguous.*region"):
        _execute(
            metrics_runtime,
            "SELECT METRIC(ratio) FROM __semantic.__table s WHERE s.region = 'north'",
        )


def test_rewrite_explicit_dimension_disambiguates_filter(metrics_runtime):
    connection, graph, _ = metrics_runtime
    graph.add_model(_model("dims.audience", "org VARCHAR, region VARCHAR", "org"))
    connection.execute(
        """
        CREATE TABLE dims.audience (org VARCHAR, region VARCHAR);
        INSERT INTO dims.audience VALUES ('A', 'south'), ('B', 'north');
        """
    )
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(a_sum), METRIC(b_sum)
        FROM __semantic.__table s
        LEFT JOIN dims.organizations o ON s.org = o.org
        WHERE o.region = 'north'
        GROUP BY s.org
        """,
    ) == [("A", 30, 10)]


def test_rewrite_unreachable_filter(metrics_runtime):
    _, graph, _ = metrics_runtime
    graph.add_model(_model("dims.remote", "remote_id INT, restricted VARCHAR", "remote_id"))
    with pytest.raises((ConfigError, SQLMeshError)):
        _execute(
            metrics_runtime,
            "SELECT METRIC(ratio) FROM __semantic.__table s WHERE s.restricted = 'yes'",
        )


@pytest.mark.parametrize(
    "predicate",
    [
        "s.org IN (SELECT org FROM dims.organizations)",
        "EXISTS (SELECT 1 FROM dims.organizations o WHERE o.org = s.org)",
    ],
)
def test_rewrite_rejects_subquery_filter(metrics_runtime, predicate):
    with pytest.raises(ConfigError, match="(?i)subquer"):
        _execute(
            metrics_runtime,
            f"SELECT METRIC(ratio) FROM __semantic.__table s WHERE {predicate}",
        )


def test_rewrite_right_only_group(metrics_runtime):
    connection, _, _ = metrics_runtime
    connection.execute("INSERT INTO facts.b VALUES (15, 'C', 'app', 7, 'fact')")
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org AS organization, METRIC(a_sum) AS total_a, METRIC(b_sum) AS total_b
        FROM __semantic.__table s
        GROUP BY s.org
        ORDER BY organization
        """,
    ) == [("A", 30, 10), ("B", 100, 50), ("C", None, 7)]


def test_rewrite_three_facts(metrics_runtime):
    connection, _, _ = metrics_runtime
    connection.execute("INSERT INTO facts.b VALUES (15, 'C', 'app', 7, 'fact')")
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(a_sum), METRIC(b_sum), METRIC(c_sum)
        FROM __semantic.__table s
        GROUP BY s.org
        ORDER BY s.org
        """,
    ) == [("A", 30, 10, None), ("B", 100, 50, 4), ("C", None, 7, 7), ("D", None, None, 8)]


def test_rewrite_composite_null_grain(metrics_runtime):
    connection, _, _ = metrics_runtime
    connection.execute(
        """
        INSERT INTO facts.a VALUES
            (4, NULL, NULL, 11, 'fact'), (5, 'A', NULL, 13, 'fact'),
            (6, NULL, 'web', 17, 'fact');
        INSERT INTO facts.b VALUES
            (15, NULL, NULL, 2, 'fact'), (16, 'A', NULL, 3, 'fact'),
            (17, NULL, 'app', 5, 'fact');
        INSERT INTO facts.c VALUES
            (24, NULL, NULL, 7, 'fact'), (25, NULL, 'app', 9, 'fact'),
            (26, 'A', NULL, 11, 'fact');
        """
    )
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, s.channel, METRIC(a_sum), METRIC(b_sum), METRIC(c_sum)
        FROM __semantic.__table s
        GROUP BY s.org, s.channel
        ORDER BY s.org NULLS FIRST, s.channel NULLS FIRST
        """,
    ) == [
        (None, None, 11, 2, 7),
        (None, "app", None, 5, 9),
        (None, "web", 17, None, None),
        ("A", None, 13, 3, 11),
        ("A", "app", 20, 8, None),
        ("A", "web", 10, 2, None),
        ("B", "web", 100, 50, 4),
        ("C", "app", None, None, 7),
        ("D", "web", None, None, 8),
    ]


def test_rewrite_computed_grain_and_output_aliases(metrics_runtime):
    connection, _, _ = metrics_runtime
    connection.execute("INSERT INTO facts.b VALUES (15, 'C', 'app', 7, 'fact')")
    assert _execute(
        metrics_runtime,
        """
        SELECT bucket, total_a, total_b
        FROM (
            SELECT CASE WHEN s.org = 'A' THEN 'first' ELSE 'other' END AS bucket,
                   METRIC(a_sum) AS total_a, METRIC(b_sum) AS total_b
            FROM __semantic.__table s
            GROUP BY CASE WHEN s.org = 'A' THEN 'first' ELSE 'other' END
        ) summary
        ORDER BY bucket DESC
        """,
    ) == [("other", 100, 57), ("first", 30, 10)]


@pytest.mark.parametrize(
    "join_type, expected",
    [
        ("FULL", [("A", 30, 10), ("B", 100, 50), ("C", None, 7), ("D", 9, None)]),
        ("LEFT", [("A", 30, 10), ("B", 100, 50), ("D", 9, None)]),
        ("INNER", [("A", 30, 10), ("B", 100, 50)]),
        ("RIGHT", [("A", 30, 10), ("B", 100, 50), ("C", None, 7)]),
    ],
)
def test_rewrite_join_type(metrics_runtime, join_type, expected):
    connection, _, _ = metrics_runtime
    connection.execute(
        """
        INSERT INTO facts.a VALUES (4, 'D', 'web', 9, 'fact');
        INSERT INTO facts.b VALUES (15, 'C', 'app', 7, 'fact');
        """
    )
    assert (
        _execute(
            metrics_runtime,
            """
        SELECT s.org, METRIC(a_sum), METRIC(b_sum)
        FROM __semantic.__table s
        GROUP BY s.org
        ORDER BY s.org
        """,
            join_type=join_type,
        )
        == expected
    )


def test_rewrite_preserves_non_metric_nested_query(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT nested.org, nested.amount + (SELECT 1) AS incremented
        FROM (SELECT org, amount FROM facts.a WHERE amount >= 20) nested
        ORDER BY nested.org, nested.amount
        """,
    ) == [("A", 21), ("B", 101)]


def test_rewrite_nested_metric_scopes(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT METRIC(a_sum),
               (SELECT METRIC(b_sum) FROM __semantic.__table WHERE org = 'A') AS b
        FROM __semantic.__table
        WHERE org = 'B'
        """,
    ) == [(100, 10)]


def test_rewrite_conditional_metric_with_dimension_alias(metrics_runtime):
    _, _, metrics = metrics_runtime
    meta = load_metric_ddl(
        d.parse_one(
            "METRIC(name active_amount, expression "
            "SUM(IF(dims.organizations.status = 'ACTIVE', facts.a.amount, 0)))"
        ),
        dialect="duckdb",
    )
    metrics.update(expand_metrics({meta.name: meta}))
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, METRIC(active_amount)
        FROM __semantic.__table s
        LEFT JOIN dims.organizations o ON s.org = o.org
        GROUP BY s.org
        ORDER BY s.org
        """,
    ) == [("A", 30), ("B", 0)]


def test_rewrite_case_insensitive_query_metric(metrics_runtime):
    connection, graph, metrics = metrics_runtime
    query = rewrite(
        'SELECT METRIC("A_SUM") FROM __semantic.__table',
        graph=graph,
        metrics=metrics,
        dialect="snowflake",
    )
    assert connection.execute(query.sql("duckdb")).fetchall() == [(130,)]


def test_rewrite_distinct_on_group_keys(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT DISTINCT ON (s.channel) s.org, s.channel, METRIC(a_sum)
        FROM __semantic.__table s
        GROUP BY s.org, s.channel
        ORDER BY s.channel, s.org
        """,
    ) == [("A", "app", 20), ("A", "web", 10)]


def test_rewrite_named_window_group_keys(metrics_runtime):
    assert _execute(
        metrics_runtime,
        """
        SELECT s.org, s.channel, METRIC(a_sum), ROW_NUMBER() OVER w AS position
        FROM __semantic.__table s
        GROUP BY s.org, s.channel
        WINDOW w AS (PARTITION BY s.channel ORDER BY s.org)
        ORDER BY s.channel, s.org
        """,
    ) == [("A", "app", 20, 1), ("A", "web", 10, 1), ("B", "web", 100, 2)]
