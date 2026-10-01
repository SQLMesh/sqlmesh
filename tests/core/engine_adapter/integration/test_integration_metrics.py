# SPDX-License-Identifier: Apache-2.0
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlglot import exp

from sqlmesh.core.config import Config, ModelDefaultsConfig
from tests.core.engine_adapter.integration import (
    ENGINES_BY_NAME,
    TestContext,
    generate_pytest_params,
)


@pytest.fixture(params=list(generate_pytest_params(ENGINES_BY_NAME["postgres"])))
def ctx(request, create_test_context):
    yield from create_test_context(*request.param)


@pytest.fixture
def metric_project(ctx: TestContext, tmp_path: Path):
    schema = exp.to_table(ctx.schema()).sql("postgres")
    models = tmp_path / "models"
    metrics = tmp_path / "metrics"
    models.mkdir()
    metrics.mkdir()
    rows = {
        "a": "(1,'A','web',10.50,'2024-02-28 23:30:00'), (2,'A','app',20.50,'2024-02-29 00:30:00'), (3,'B','web',100.00,'2024-03-01 00:00:00'), (4,NULL,NULL,11.00,'2024-03-01 00:00:00')",
        "b": "(11,'A','web',2.00,'2024-02-28 23:30:00'), (12,'A','app',3.00,'2024-02-29 00:30:00'), (13,'A','app',5.00,'2024-02-29 01:30:00'), (14,'B','web',50.00,'2024-03-01 00:00:00'), (15,'C','app',7.00,'2024-03-02 00:00:00'), (16,NULL,NULL,2.00,'2024-03-01 00:00:00')",
        "c": "(21,'B','web',4.00,'2024-03-01 00:00:00'), (22,'C','app',7.00,'2024-03-02 00:00:00'), (23,'D','web',8.00,'2024-03-03 00:00:00'), (24,NULL,NULL,3.00,'2024-03-01 00:00:00')",
    }
    for name, values in rows.items():
        (models / f"{name}.sql").write_text(
            f"""
            MODEL (name {schema}.{name}, kind FULL, grain {name}_id, references org);
            SELECT id::INT AS {name}_id, org::TEXT AS org, channel::TEXT AS channel,
                   amount::DECIMAL(12,2) AS amount, occurred_at::TIMESTAMP AS occurred_at
            FROM (VALUES {values}) AS data(id, org, channel, amount, occurred_at)
            """
        )
    (models / "organizations.sql").write_text(
        f"""
        MODEL (name {schema}.organizations, kind FULL, grain org);
        SELECT org::TEXT AS org, region::TEXT AS region, status::TEXT AS status
        FROM (VALUES ('A','north','ACTIVE'),('B','south','INACTIVE'),
                     ('C','north','ACTIVE'),('D','south','INACTIVE')) AS data(org,region,status)
        """
    )
    (models / "quoted.sql").write_text(
        f"""
        MODEL (name {schema}.\"QuotedFacts\", kind FULL, grain \"Id\");
        SELECT 1 AS "Id", 'A'::TEXT AS "Org", 1.25::DECIMAL(12,2) AS "Amount"
        """
    )
    (metrics / "metrics.sql").write_text(
        "\n".join(
            f"METRIC(name {name}_sum, expression SUM({schema}.{name}.amount));" for name in rows
        )
        + f"""
        METRIC(name ratio, expression a_sum / b_sum);
        METRIC(name a_count, expression COUNT({schema}.a.a_id));
        METRIC(name b_count, expression COUNT({schema}.b.b_id));
        METRIC(name count_ratio, expression CAST(a_count AS DOUBLE) / NULLIF(b_count, 0));
        METRIC(name active_amount, expression SUM(IF({schema}.organizations.status = 'ACTIVE', {schema}.a.amount, 0)));
        METRIC(name quoted_amount, expression SUM({schema}."QuotedFacts"."Amount"));
        """
    )

    def configure(gateway: str, config: Config) -> None:
        config.model_defaults = ModelDefaultsConfig(dialect="postgres")

    context = ctx.create_context(path=tmp_path, config_mutator=configure)
    try:
        context.plan(auto_apply=True, no_prompts=True)
        yield context, schema
    finally:
        context.close()


def _query(project, sql):
    context, _ = project
    return context.engine_adapter.fetchall(context.rewrite(sql).sql("postgres"))


def test_metrics_postgres_filtered_ratio(metric_project):
    assert _query(
        metric_project,
        "SELECT METRIC(a_sum), METRIC(b_sum), METRIC(ratio), METRIC(count_ratio) "
        "FROM __semantic.__table s WHERE s.org = 'A'",
    ) == [(Decimal("31.00"), Decimal("10.00"), Decimal("3.1"), pytest.approx(2 / 3))]
    assert _query(
        metric_project,
        "SELECT s.org, METRIC(a_sum), METRIC(b_sum), METRIC(ratio) "
        "FROM __semantic.__table s WHERE s.channel='web' GROUP BY s.org ORDER BY s.org",
    ) == [
        ("A", Decimal("10.50"), Decimal("2.00"), Decimal("5.25")),
        ("B", Decimal("100.00"), Decimal("50.00"), Decimal("2.0")),
    ]


def test_metrics_postgres_empty_scope(metric_project):
    assert _query(
        metric_project,
        "SELECT METRIC(a_count), METRIC(b_count), METRIC(ratio), METRIC(count_ratio) "
        "FROM __semantic.__table s WHERE s.org = 'missing'",
    ) == [(0, 0, None, None)]


def test_metrics_postgres_three_facts_null_keys(metric_project):
    assert _query(
        metric_project,
        "SELECT s.org, s.channel, METRIC(a_sum), METRIC(b_sum), METRIC(c_sum) "
        "FROM __semantic.__table s GROUP BY s.org,s.channel "
        "ORDER BY s.org NULLS FIRST,s.channel NULLS FIRST",
    ) == [
        (None, None, Decimal("11"), Decimal("2"), Decimal("3")),
        ("A", "app", Decimal("20.5"), Decimal("8"), None),
        ("A", "web", Decimal("10.5"), Decimal("2"), None),
        ("B", "web", Decimal("100"), Decimal("50"), Decimal("4")),
        ("C", "app", None, Decimal("7"), Decimal("7")),
        ("D", "web", None, None, Decimal("8")),
    ]


def test_metrics_postgres_dimension_filter(metric_project):
    _, schema = metric_project
    assert _query(
        metric_project,
        f"SELECT s.org,METRIC(a_sum),METRIC(b_sum),METRIC(active_amount) "
        f"FROM __semantic.__table s LEFT JOIN {schema}.organizations o ON s.org=o.org "
        "WHERE o.region='north' GROUP BY s.org ORDER BY s.org",
    ) == [("A", Decimal("31"), Decimal("10"), Decimal("31")), ("C", None, Decimal("7"), None)]


def test_metrics_postgres_time_buckets(metric_project):
    assert _query(
        metric_project,
        "SELECT DATE_TRUNC('day',s.occurred_at) AS day,METRIC(a_sum),METRIC(b_sum) "
        "FROM __semantic.__table s "
        "WHERE s.occurred_at >= CAST('2024-02-29' AS TIMESTAMP) "
        "AND s.occurred_at < CAST('2024-03-02' AS TIMESTAMP) "
        "GROUP BY DATE_TRUNC('day',s.occurred_at) ORDER BY day",
    ) == [
        (datetime(2024, 2, 29), Decimal("20.5"), Decimal("8")),
        (datetime(2024, 3, 1), Decimal("111"), Decimal("52")),
    ]


def test_metrics_postgres_quoted_identifiers(metric_project):
    assert _query(
        metric_project,
        'SELECT s."Org" AS "Organization", METRIC("QUOTED_AMOUNT") AS "Value" '
        'FROM __semantic.__table s WHERE s."Org"=\'A\' GROUP BY s."Org"',
    ) == [("A", Decimal("1.25"))]


def test_metrics_postgres_distinct_on(metric_project):
    assert _query(
        metric_project,
        "SELECT DISTINCT ON (s.channel) s.org, METRIC(a_sum) "
        "FROM __semantic.__table s GROUP BY s.org,s.channel "
        "ORDER BY s.channel,s.org NULLS FIRST",
    ) == [("A", Decimal("20.5")), ("A", Decimal("10.5")), (None, Decimal("11"))]
