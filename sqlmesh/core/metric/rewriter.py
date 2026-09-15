from __future__ import annotations

import typing as t

from sqlglot import exp
from sqlglot.dialects.dialect import DialectType
from sqlglot.optimizer import Scope, find_all_in_scope, optimize
from sqlglot.optimizer.optimize_joins import optimize_joins
from sqlglot.optimizer.qualify import qualify

from sqlmesh.core import dialect as d
from sqlmesh.core.metric.definition import Metric, remove_namespace

if t.TYPE_CHECKING:
    from sqlmesh.core.reference import ReferenceGraph


SourceAggsAndJoins = t.Dict[str, t.Tuple[t.Set[exp.AggFunc], t.Dict[str, t.Optional[exp.Join]]]]


class Rewriter:
    def __init__(
        self,
        graph: ReferenceGraph,
        metrics: t.Dict[str, Metric],
        dialect: DialectType = "",
        join_type: str = "FULL",
        semantic_schema: str = "__semantic",
        semantic_table: str = "__table",
    ):
        self.graph = graph
        self.metrics = metrics
        self.dialect = dialect
        self.join_type = join_type
        self.semantic_name = f"{semantic_schema}.{semantic_table}"

    def rewrite(self, expression: exp.Expr) -> exp.Expr:
        for select in list(expression.find_all(exp.Select)):
            self._expand(select)

        return expression

    def _build_sources(self, projections: t.List[exp.Expr]) -> SourceAggsAndJoins:
        sources: SourceAggsAndJoins = {}

        for projection in projections:
            for ref in find_all_in_scope(projection, d.MetricAgg):
                metric = self.metrics[ref.this.name]
                ref.replace(metric.formula.this)

                for agg, (measure, dims) in metric.aggs.items():
                    aggs, joins = sources.setdefault(measure, (set(), dict()))
                    aggs.add(agg)
                    for dim in dims:
                        joins[dim] = None

        return sources

    def _expand(self, select: exp.Select) -> None:
        base = select.args["from_"].this.find(exp.Table)
        base_alias = base.alias_or_name
        base_name = exp.table_name(base)

        sources: SourceAggsAndJoins = (
            {} if base_name == self.semantic_name else {base_name: (set(), {})}
        )
        sources.update(self._build_sources(select.selects))

        group = select.args.pop("group", None)
        group_by = group.expressions if group else []

        mapping = {
            remove_namespace(exp.table_name(source.assert_is(exp.Table))): name
            for name, source in Scope(select).references
            if name != base_alias
        }

        explicit_joins = {exp.table_name(join.this): join for join in select.args.pop("joins", [])}

        for i, (name, (aggs, joins)) in enumerate(sources.items()):
            source: exp.Expr = exp.to_table(name)
            table_name = remove_namespace(name)

            if not isinstance(source, exp.Select):
                source = exp.Select().from_(
                    exp.alias_(source, table_name, table=True, copy=False), copy=False
                )

            joins.update(explicit_joins)
            query = self._add_joins(source, name, joins, group_by, mapping).select(
                *sorted(aggs, key=str), copy=False
            )

            if not query.selects:
                query.select("*", copy=False)

            if i == 0:
                where = select.args.pop("where", None)

                if where:
                    query.where(_replace_table(where.this, table_name, base_alias), copy=False)

                select.from_(query.subquery(base_alias, copy=False), copy=False)
            else:
                select.join(
                    query,
                    on=[e.eq(_replace_table(e.copy(), table_name, base_alias)) for e in group_by],  # type: ignore
                    join_type=self.join_type,
                    join_alias=table_name,
                    copy=False,
                )

            for node in find_all_in_scope(query, exp.Column, exp.TableAlias):  # type: ignore[arg-type,var-annotated]
                if isinstance(node, exp.Column):
                    if node.table in mapping:
                        node.set("table", exp.to_identifier(mapping[node.table]))
                else:
                    if node.name in mapping:
                        node.set("this", exp.to_identifier(mapping[node.name]))

    def _add_joins(
        self,
        source: exp.Select,
        name: str,
        joins: t.Dict[str, t.Optional[exp.Join]],
        group_by: t.List[exp.Expr],
        mapping: t.Dict[str, str],
    ) -> exp.Select:
        grain = [e.copy() for e in group_by]
        table_name = remove_namespace(name)
        mapping = {v: k for k, v in mapping.items()}

        for expr in grain:
            for node in expr.walk():
                if isinstance(node, exp.Column):
                    models = self.graph.models_for_column(name, node.name)

                    if name in models:
                        node.args["table"] = exp.to_identifier(table_name)
                    elif models:
                        t = mapping.get(node.table)
                        model = next(
                            (model for model in models if remove_namespace(model) == t),
                            models[0],
                        )
                        node.args["table"] = exp.to_identifier(t or remove_namespace(model))
                        if model not in joins:
                            joins[model] = None

        for target, join in joins.items():
            path = self.graph.find_path(name, target)
            for i in range(len(path) - 1):
                a_ref = path[i]
                b_ref = path[i + 1]
                a_model_alias = remove_namespace(a_ref.model_name)
                b_model_alias = remove_namespace(b_ref.model_name)

                a = a_ref.expression.copy()
                a.set("table", exp.to_identifier(a_model_alias))
                b = b_ref.expression.copy()
                b.set("table", exp.to_identifier(b_model_alias))
                on = a.eq(b)

                if join:
                    join.set("on", on)
                    source.append("joins", join)
                else:
                    source.join(
                        b_ref.model_name,
                        on=on,
                        join_type="LEFT",
                        join_alias=b_model_alias,
                        dialect=self.dialect,
                        copy=False,
                    )

        return source.select(*grain, copy=False).group_by(*grain, copy=False)


def _replace_table(node: exp.Expr, table: str, base_alias: str) -> exp.Expr:
    for column in find_all_in_scope(node, exp.Column):
        if column.table == base_alias:
            column.args["table"] = exp.to_identifier(table)
    return node


def rewrite(
    sql: str | exp.Expr,
    graph: ReferenceGraph,
    metrics: t.Dict[str, Metric],
    dialect: t.Optional[str] = "",
) -> exp.Expr:
    rewriter = Rewriter(graph=graph, metrics=metrics, dialect=dialect)

    return optimize(
        d.parse_one(sql, dialect=dialect) if isinstance(sql, str) else sql,
        dialect=dialect,
        quote_identifiers=False,
        rules=(
            qualify,
            rewriter.rewrite,
            optimize_joins,
        ),
    )
