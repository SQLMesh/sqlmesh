from pathlib import Path

import pytest

from sqlmesh.core import dialect as d
from sqlmesh.core.metric import expand_metrics, load_metric_ddl, rewrite
from sqlmesh.core.reference import ReferenceGraph
from sqlmesh.utils import UniqueKeyDict
from sqlmesh.utils.errors import ConfigError


@pytest.mark.parametrize(
    "statement, tokens",
    [
        ("SELECT 1", ("METRIC", "SELECT")),
        ("METRIC(name invalid, expression 1)", ("invalid", "aggregation", "ref")),
    ],
)
def test_load_invalid(statement, tokens):
    path = Path("metrics/invalid.sql")
    with pytest.raises(ConfigError) as exc_info:
        load_metric_ddl(d.parse_one(statement), dialect="duckdb", path=path).to_metric({}, {})

    message = str(exc_info.value)
    assert str(path) in message
    assert all(token in message for token in tokens)


def _load_metas(definitions, dialect="duckdb"):
    metas = UniqueKeyDict("metrics")
    for name, expression in definitions:
        meta = load_metric_ddl(
            d.parse_one(f"METRIC(name {name}, expression {expression})", dialect=dialect),
            dialect=dialect,
            path=Path("metrics") / f"{name.lower()}.sql",
        )
        metas[meta.name] = meta
    return metas


def _execute_metrics(metrics, names):
    import duckdb

    query = rewrite(
        "SELECT " + ", ".join(f"METRIC({name})" for name in names) + " FROM __semantic.__table",
        graph=ReferenceGraph([]),
        metrics=metrics,
        dialect="duckdb",
    )
    with duckdb.connect(":memory:") as connection:
        connection.execute("CREATE TABLE facts(amount INT, category VARCHAR)")
        connection.execute("INSERT INTO facts VALUES (10, 'a'), (15, 'a'), (5, 'b')")
        return connection.execute(query.sql(dialect="duckdb")).fetchall()


@pytest.mark.parametrize("direct", [False, True])
def test_forward_and_diamond_dependencies(direct):
    metas = _load_metas(
        [
            ("root", "ratio + adjusted + total / count"),
            ("ratio", "total / count"),
            ("adjusted", "ratio + 1"),
            ("total", "SUM(facts.amount)"),
            ("count", "COUNT(DISTINCT facts.category)"),
        ]
    )
    if direct:
        root = metas.pop("root")
        metrics = UniqueKeyDict("metrics")
        metrics[root.name] = root.to_metric(metas, metrics)
    else:
        metrics = expand_metrics(metas)

    assert _execute_metrics(metrics, ["root", "ratio", "adjusted", "total", "count"]) == [
        (46.0, 15.0, 16.0, 30, 2)
    ]


@pytest.mark.parametrize("dialect", ["duckdb", "snowflake"])
def test_case_insensitive_metric_dependencies(dialect):
    metas = _load_metas(
        [
            ("RaTiO", 'ToTaL / "COUNT"'),
            ("TOTAL", "SUM(facts.amount)"),
            ("Count", "COUNT(DISTINCT facts.category)"),
        ],
        dialect=dialect,
    )

    assert _execute_metrics(expand_metrics(metas), ["ratio", "total", "count"]) == [(15.0, 30, 2)]


def test_direct_expansion_reuses_resolved_dependencies():
    total = _load_metas([("total", "SUM(facts.amount)")])["total"]
    metrics = UniqueKeyDict("metrics")
    metrics[total.name] = total.to_metric({}, metrics)
    doubled = _load_metas([("doubled", "total + total")])["doubled"]
    metrics[doubled.name] = doubled.to_metric({}, metrics)

    assert _execute_metrics(metrics, ["total", "doubled"]) == [(30, 60)]


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "definitions, dependency_path, source_path, diagnostic",
    [
        (
            [("root", "root")],
            "root -> root",
            "metrics/root.sql",
            "cycle",
        ),
        (
            [("root", "x"), ("x", "y"), ("y", "x")],
            "root -> x -> y -> x",
            "metrics/y.sql",
            "cycle",
        ),
        (
            [("root", "intermediate"), ("intermediate", "missing_metric + 1")],
            "root -> intermediate -> missing_metric",
            "metrics/intermediate.sql",
            "unknown",
        ),
    ],
    ids=["self_cycle", "nested_cycle", "unknown_dependency"],
)
def test_invalid_metric_dependencies(direct, definitions, dependency_path, source_path, diagnostic):
    metas = _load_metas(definitions)

    with pytest.raises(ConfigError) as exc_info:
        if direct:
            root = metas.pop("root")
            root.to_metric(metas, UniqueKeyDict("metrics"))
        else:
            expand_metrics(metas)

    message = str(exc_info.value)
    assert diagnostic in message.lower()
    assert dependency_path in message
    assert source_path in message


def test_long_dependency_cycle():
    depth = 1100
    metas = _load_metas([(f"m{i}", f"m{(i + 1) % depth}") for i in range(depth)])

    with pytest.raises(ConfigError) as exc_info:
        expand_metrics(metas)

    message = str(exc_info.value)
    assert "cycle" in message.lower()
    assert "m0 -> m1 -> m2" in message
    assert "m1099 -> m0" in message
    assert "metrics/m1099.sql" in message
