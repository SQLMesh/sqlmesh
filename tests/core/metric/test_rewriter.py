from sqlglot import exp, parse_one

from sqlmesh.core import dialect as d
from sqlmesh.core.metric import expand_metrics, load_metric_ddl, rewrite
from sqlmesh.core.model import load_sql_based_model
from sqlmesh.core.reference import ReferenceGraph
from sqlmesh.utils import UniqueKeyDict


def test_rewrite(sushi_context_pre_scheduling, assert_exp_eq):
    context = sushi_context_pre_scheduling
    graph = ReferenceGraph(context.models.values())

    query = rewrite(
        """
        SELECT
          c.customer_id,
          METRIC(total_orders),
        FROM sushi.customers AS c
        GROUP BY c.customer_id
        """,
        graph=graph,
        metrics=context.metrics,
    )
    assert_exp_eq(
        query,
        parse_one(
            """
            SELECT
              c.customer_id AS customer_id,
              total_orders AS total_orders
            FROM (
              SELECT
                sushi__customers.customer_id
              FROM sushi.customers AS sushi__customers
              GROUP BY
                sushi__customers.customer_id
            ) AS c
            FULL JOIN (
              SELECT
                sushi__orders.customer_id,
                COUNT(sushi__orders.id) AS total_orders
              FROM sushi.orders AS sushi__orders
              GROUP BY
                sushi__orders.customer_id
            ) AS sushi__orders
              ON c.customer_id = sushi__orders.customer_id
            """,
            dialect=context.config.dialect,
        ),
    )

    # query = rewrite(
    #    """
    #    SELECT
    #      event_date,
    #      METRIC(total_orders),
    #      METRIC(items_per_order),
    #      METRIC(total_orders_from_active_customers),
    #    FROM __semantic.__table
    #    GROUP BY event_date
    #    """,
    #    graph=graph,
    #    metrics=context.metrics,
    # )

    # assert_exp_eq(
    #    query,
    #    parse_one(
    #        """
    #        SELECT
    #          __table.event_date AS event_date,
    #          total_orders AS total_orders,
    #          total_ordered_items / total_orders AS items_per_order,
    #          total_orders_from_active_customers AS total_orders_from_active_customers
    #        FROM (
    #          SELECT
    #            sushi__orders.event_date,
    #            COUNT(CASE WHEN sushi__customers.status = 'ACTIVE' THEN sushi__orders.id ELSE NULL END) AS total_orders_from_active_customers,
    #            COUNT(sushi__orders.id) AS total_orders
    #          FROM sushi.orders AS sushi__orders
    #          LEFT JOIN sushi.customers AS sushi__customers
    #            ON sushi__orders.customer_id = sushi__customers.customer_id
    #          GROUP BY
    #            sushi__orders.event_date
    #        ) AS __table
    #        FULL JOIN (
    #          SELECT
    #            sushi__order_items.event_date,
    #            SUM(sushi__order_items.quantity) AS total_ordered_items
    #          FROM sushi.order_items AS sushi__order_items
    #          GROUP BY
    #            sushi__order_items.event_date
    #        ) AS sushi__order_items
    #          ON __table.event_date = sushi__order_items.event_date

    #        """,
    #        dialect=context.config.dialect,
    #    ),
    # )

    # query = rewrite(
    #    """
    #    SELECT
    #      event_date,
    #      METRIC(total_orders),
    #      METRIC(items_per_order),
    #      METRIC(total_orders_from_active_customers),
    #    FROM sushi.orders
    #    GROUP BY event_date
    #    """,
    #    graph=graph,
    #    metrics=context.metrics,
    # )

    # assert_exp_eq(
    #    query,
    #    parse_one(
    #        """
    #        SELECT
    #          orders.event_date AS event_date,
    #          orders.total_orders AS total_orders,
    #          orders.total_ordered_items / orders.total_orders AS items_per_order,
    #          orders.total_orders_from_active_customers AS total_orders_from_active_customers
    #        FROM (
    #          SELECT
    #            sushi__orders.event_date,
    #            COUNT(IF(sushi__customers.status = 'ACTIVE', sushi__orders.id, NULL)) AS total_orders_from_active_customers,
    #            COUNT(sushi__orders.id) AS total_orders
    #          FROM sushi.orders AS sushi__orders
    #          LEFT JOIN sushi.customers AS sushi__customers
    #            ON sushi__orders.customer_id = sushi__customers.customer_id
    #          GROUP BY
    #            sushi__orders.event_date
    #        ) AS orders
    #        FULL JOIN (
    #          SELECT
    #            sushi__order_items.event_date,
    #            SUM(sushi__order_items.quantity) AS total_ordered_items
    #          FROM sushi.order_items AS sushi__order_items
    #          GROUP BY
    #            sushi__order_items.event_date
    #        ) AS sushi__order_items
    #          ON orders.event_date = sushi__order_items.event_date

    #        """,
    #        dialect=context.config.dialect,
    #    ),
    # )

    # query = rewrite(
    #    """
    #    SELECT
    #      event_date,
    #      status,
    #      METRIC(total_orders),
    #    FROM __semantic.__table
    #    GROUP BY event_date, status
    #    """,
    #    graph=graph,
    #    metrics=context.metrics,
    # )
    # assert_exp_eq(
    #    query,
    #    parse_one(
    #        """
    #        SELECT
    #          __table.event_date AS event_date,
    #          __table.status AS status,
    #          __table.total_orders AS total_orders
    #        FROM (
    #          SELECT
    #            sushi__orders.event_date,
    #            sushi__customers.status,
    #            COUNT(sushi__orders.id) AS total_orders
    #          FROM sushi.orders AS sushi__orders
    #          LEFT JOIN sushi.customers AS sushi__customers
    #            ON sushi__orders.customer_id = sushi__customers.customer_id
    #          GROUP BY
    #            sushi__orders.event_date,
    #            sushi__customers.status
    #        ) AS __table
    #        """,
    #        dialect=context.config.dialect,
    #    ),
    # )

    # query = rewrite(
    #    """
    #    SELECT
    #      t.event_date,
    #      m.status,
    #      METRIC(t.total_orders),
    #    FROM __semantic.__table t
    #    LEFT JOIN sushi.raw_marketing AS m
    #    WHERE t.event_date > '2022-01-01'
    #    GROUP BY t.event_date, m.status
    #    """,
    #    graph=graph,
    #    metrics=context.metrics,
    # )
    # assert_exp_eq(
    #    query,
    #    parse_one(
    #        """
    #        SELECT
    #          t.event_date AS event_date,
    #          t.status AS status,
    #          t.total_orders AS total_orders
    #        FROM (
    #          SELECT
    #            sushi__orders.event_date,
    #            m.status,
    #            COUNT(sushi__orders.id) AS total_orders
    #          FROM sushi.orders AS sushi__orders
    #          LEFT JOIN sushi.raw_marketing AS m
    #            ON sushi__orders.customer_id = m.customer_id
    #          WHERE
    #            sushi__orders.event_date > '2022-01-01'
    #          GROUP BY
    #            sushi__orders.event_date,
    #            m.status
    #        ) AS t
    #        """,
    #        dialect=context.config.dialect,
    #    ),
    # )


def test_rewrite_order_by_metric(sushi_context_pre_scheduling):
    context = sushi_context_pre_scheduling
    graph = ReferenceGraph(context.models.values())
    meta = load_metric_ddl(
        d.parse_one(
            "METRIC (name sorted_amounts, expression ARRAY_AGG(sushi.orders.amount ORDER BY sushi.orders.amount))",
            dialect=context.config.dialect,
        ),
        dialect=context.config.dialect,
    )
    metrics = expand_metrics(UniqueKeyDict("metas", {meta.name: meta}))

    query = rewrite(
        "SELECT c.customer_id, METRIC(sorted_amounts) FROM sushi.customers AS c GROUP BY c.customer_id",
        graph=graph,
        metrics=metrics,
    )
    sql = query.sql()
    # Stop before the generated NULLS LAST so the assertion doesn't depend on null ordering.
    assert "ARRAY_AGG(sushi__orders.amount ORDER BY sushi__orders.amount" in sql
    assert "sushi.orders AS sushi__orders" in sql
    parse_one(sql, dialect=context.config.dialect)


def test_rewrite_nested_metric_aggregates():
    dialect = "bigquery"
    model = load_sql_based_model(
        d.parse(
            "MODEL (name sushi.orders, kind FULL); SELECT 1 AS customer_id, 10 AS amount, 100 AS other_amount, 1000 AS __sqlmesh_metric_median_amount__agg_0",
            default_dialect=dialect,
        ),
        dialect=dialect,
    )
    metric_statements = d.parse(
        """
        METRIC (name total_amount, expression SUM(sushi.orders.amount));
        METRIC (name derived_amount, expression total_amount + 1);
        METRIC (name median_amount, expression APPROX_QUANTILES(sushi.orders.amount, 2)[OFFSET(1)]);
        METRIC (name safe_median_amount, expression COALESCE(CAST(APPROX_QUANTILES(sushi.orders.amount, 2)[OFFSET(1)] AS FLOAT64), 0));
        METRIC (name scaled_median, expression median_amount * 2);
        METRIC (name safe_median_plus_one, expression safe_median_amount + 1);
        METRIC (name combined_amount, expression SUM(sushi.orders.amount) + MAX(sushi.orders.amount));
        METRIC (name reused_amount, expression SUM(sushi.orders.amount) / NULLIF(SUM(sushi.orders.amount), 0));
        METRIC (name __sqlmesh_metric_safe_median_amount__agg_0, expression SUM(sushi.orders.other_amount));
        """,
        default_dialect=dialect,
    )
    metas = UniqueKeyDict(
        "metas",
        {
            meta.name: meta
            for meta in (load_metric_ddl(stmt, dialect=dialect) for stmt in metric_statements)
        },
    )
    metrics = expand_metrics(metas)
    graph = ReferenceGraph([model])

    query = rewrite(
        """
        SELECT
          customer_id,
          METRIC(median_amount),
          METRIC(safe_median_amount),
          METRIC(scaled_median),
          METRIC(safe_median_plus_one),
          METRIC(combined_amount),
          METRIC(reused_amount),
          METRIC(__sqlmesh_metric_safe_median_amount__agg_0),
          METRIC(total_amount),
          METRIC(derived_amount)
        FROM __semantic.__table
        GROUP BY customer_id
        """,
        graph=graph,
        metrics=metrics,
        dialect=dialect,
    )

    outer_projections = {
        projection.alias_or_name: (
            projection.this if isinstance(projection, exp.Alias) else projection
        )
        for projection in query.selects
    }
    assert isinstance(outer_projections["median_amount"], exp.Bracket)
    safe_median = outer_projections["safe_median_amount"]
    assert isinstance(safe_median, exp.Coalesce)
    assert safe_median.find(exp.Cast) is not None
    assert safe_median.find(exp.Bracket) is not None

    scaled_median = outer_projections["scaled_median"]
    assert isinstance(scaled_median, exp.Mul)
    assert isinstance(scaled_median.this, exp.Bracket)
    safe_median_plus_one = outer_projections["safe_median_plus_one"]
    assert isinstance(safe_median_plus_one, exp.Add)
    assert isinstance(safe_median_plus_one.this, exp.Coalesce)
    assert safe_median_plus_one.find(exp.Cast) is not None

    median_aggregate_ref = outer_projections["median_amount"].this
    safe_median_aggregate_ref = outer_projections["safe_median_amount"].find(exp.Bracket).this
    assert median_aggregate_ref.name != safe_median_aggregate_ref.name

    combined = outer_projections["combined_amount"]
    assert isinstance(combined, exp.Add)
    assert not list(combined.find_all(exp.AggFunc))
    reused = outer_projections["reused_amount"]
    reused_aggregate_refs = list(reused.find_all(exp.Column))
    assert len(reused_aggregate_refs) == 2
    assert reused_aggregate_refs[0].name == reused_aggregate_refs[1].name

    # The aggregate expressions are projected once, with aliases that the outer
    # metric formulas can reference after applying their wrappers.
    source = query.args["from_"].this
    inner = source.this if isinstance(source, exp.Subquery) else None
    assert isinstance(inner, exp.Select)
    aggregate_projections = [
        projection for projection in inner.selects if projection.find(exp.AggFunc)
    ]
    assert len(aggregate_projections) >= 3
    assert all(isinstance(projection, exp.Alias) for projection in aggregate_projections)

    # Existing simple and derived metric behavior remains intact.
    assert isinstance(outer_projections["total_amount"], exp.Column)
    assert isinstance(outer_projections["derived_amount"], exp.Add)

    # A public metric name can match the generated alias for an aggregate
    # nested under a wrapper. The source query must allocate distinct aliases.
    colliding_metric = outer_projections["__sqlmesh_metric_safe_median_amount__agg_0"]
    assert isinstance(colliding_metric, exp.Column)
    assert colliding_metric.name != safe_median_aggregate_ref.name
    projected_aliases = [projection.alias for projection in aggregate_projections]
    assert len(projected_aliases) == len(set(projected_aliases))

    dimension_metrics = {"median_amount": metrics["median_amount"]}
    selected_dimension_query = rewrite(
        """
        SELECT __sqlmesh_metric_median_amount__agg_0, METRIC(median_amount)
        FROM __semantic.__table
        GROUP BY __sqlmesh_metric_median_amount__agg_0
        """,
        graph=graph,
        metrics=dimension_metrics,
        dialect=dialect,
    )
    selected_dimension_source = selected_dimension_query.args["from_"].this
    selected_dimension_inner = (
        selected_dimension_source.this
        if isinstance(selected_dimension_source, exp.Subquery)
        else None
    )
    assert isinstance(selected_dimension_inner, exp.Select)
    selected_dimension_names = [
        projection.output_name for projection in selected_dimension_inner.selects
    ]
    assert len(selected_dimension_names) == len(set(selected_dimension_names))

    dimension_collision_query = rewrite(
        """
        SELECT METRIC(median_amount)
        FROM __semantic.__table
        GROUP BY __sqlmesh_metric_median_amount__agg_0
        """,
        graph=graph,
        metrics=dimension_metrics,
        dialect=dialect,
    )
    dimension_source = dimension_collision_query.args["from_"].this
    dimension_inner = dimension_source.this if isinstance(dimension_source, exp.Subquery) else None
    assert isinstance(dimension_inner, exp.Select)
    dimension_projection_names = [projection.output_name for projection in dimension_inner.selects]
    assert len(dimension_projection_names) == len(set(dimension_projection_names))
