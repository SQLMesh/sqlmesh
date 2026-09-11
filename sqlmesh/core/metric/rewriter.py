from __future__ import annotations

import typing as t

from sqlglot import Dialect, exp
from sqlglot.dialects.dialect import DialectType
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import OptimizeError
from sqlglot.optimizer import find_all_in_scope, optimize
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.optimize_joins import optimize_joins
from sqlglot.optimizer.qualify import qualify

from sqlmesh.core import dialect as d
from sqlmesh.core.metric.definition import Metric, remove_namespace
from sqlmesh.utils.errors import ConfigError

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
        self.semantic_name = exp.table_name(
            normalize_identifiers(
                exp.to_table(f"{semantic_schema}.{semantic_table}"), dialect=dialect
            )
        )

    def _group_join_condition(self, left: t.List[exp.Expr], right: t.List[exp.Expr]) -> exp.Expr:
        if not left:
            return exp.true()
        if type(Dialect.get_or_raise(self.dialect)) is Postgres:
            # PostgreSQL cannot plan a FULL JOIN on IS NOT DISTINCT FROM.
            # Composite equality is merge-joinable and treats NULL fields as equal,
            # unlike SQL row-constructor comparison without the RECORD casts.
            return exp.EQ(
                this=exp.Cast(
                    this=exp.Anonymous(this="ROW", expressions=[key.copy() for key in left]),
                    to=exp.DataType.build("record", dialect="postgres", udt=True),
                ),
                expression=exp.Cast(
                    this=exp.Anonymous(this="ROW", expressions=[key.copy() for key in right]),
                    to=exp.DataType.build("record", dialect="postgres", udt=True),
                ),
            )
        return exp.and_(
            *(exp.NullSafeEQ(this=a.copy(), expression=b.copy()) for a, b in zip(left, right))
        )

    def rewrite(self, expression: exp.Expr) -> exp.Expr:
        for select in reversed(list(expression.find_all(exp.Select))):
            if next(find_all_in_scope(select, d.MetricAgg), None) is not None:
                self._expand(select)

        return expression

    def _build_sources(self, projections: t.List[exp.Expr]) -> SourceAggsAndJoins:
        sources: SourceAggsAndJoins = {}

        for projection in projections:
            for ref in find_all_in_scope(projection, d.MetricAgg):
                metric = self.metrics.get(ref.this.name.lower())
                if metric is None:
                    raise ConfigError(f"Unknown metric '{ref.this.name}'")
                ref.replace(metric.formula.this)

                for agg, (measure, dims) in metric.aggs.items():
                    aggs, joins = sources.setdefault(measure, (set(), dict()))
                    aggs.add(agg)
                    for dim in dims:
                        joins[dim] = None

        return sources

    def _expand(self, select: exp.Select) -> None:
        from_ = select.args.get("from_")
        if from_ is None or not isinstance(from_.this, exp.Table):
            raise ConfigError("Metric queries require a table source")
        base = from_.this
        base_alias = base.alias_or_name
        base_name = exp.table_name(base)
        logical_alias = base_alias if base_name == self.semantic_name else ""

        where = select.args.get("where")
        if where is not None and where.find(exp.Query) is not None:
            raise ConfigError("Subqueries in metric WHERE filters are not supported")

        sources: SourceAggsAndJoins = {} if logical_alias else {base_name: (set(), {})}
        sources.update(self._build_sources(select.selects))
        if next(find_all_in_scope(select, d.MetricAgg), None) is not None:
            raise ConfigError("METRIC references must appear in the SELECT projections")

        group = select.args.pop("group", None)
        group_by = group.expressions if group else []
        if group and any(value for key, value in group.args.items() if key != "expressions"):
            raise ConfigError("Grouping sets are not supported in metric queries")

        explicit_joins = {}
        aliases = {} if logical_alias else {base_alias: base_name}
        for join in select.args.pop("joins", []):
            if not isinstance(join.this, exp.Table):
                raise ConfigError("Metric dimension joins require table sources")
            target = exp.table_name(join.this)
            if target in explicit_joins or target == base_name:
                raise ConfigError(f"Ambiguous metric dimension join to '{target}'")
            explicit_joins[target] = join
            aliases[join.this.alias_or_name] = target

        select.set("where", None)
        # Stable private names let computed grains be joined as values, not re-evaluated
        # against aggregate rows, and avoid collisions with named metric aggregates.
        used_names = {agg.alias_or_name for aggs, _ in sources.values() for agg in aggs}
        group_names = []
        for i in range(len(group_by)):
            key = f"__metric_group_{i}"
            while key in used_names:
                key += "_"
            used_names.add(key)
            group_names.append(key)

        merged_keys: t.List[exp.Expr] = []
        for i, (name, (aggs, joins)) in enumerate(sources.items()):
            table_name = remove_namespace(name)
            source_aliases = {model: alias for alias, model in aliases.items()}
            source_aliases[name] = table_name
            joins.update({target: join.copy() for target, join in explicit_joins.items()})
            grain = [
                self._resolve_columns(e.copy(), name, logical_alias, aliases, source_aliases, joins)
                for e in group_by
            ]
            predicate = (
                self._resolve_columns(
                    where.this.copy(), name, logical_alias, aliases, source_aliases, joins
                )
                if where is not None
                else None
            )
            for join in list(joins.values()):
                if join is not None and join.args.get("on") is not None:
                    join.set(
                        "on",
                        self._resolve_columns(
                            join.args["on"], name, logical_alias, aliases, source_aliases, joins
                        ),
                    )

            query = exp.select().from_(
                exp.alias_(exp.to_table(name), table_name, table=True, copy=False), copy=False
            )
            self._add_joins(query, name, joins, source_aliases)
            query.select(
                *(exp.alias_(e, key, copy=False) for e, key in zip(grain, group_names)),
                *sorted(aggs, key=str),
                copy=False,
            )
            if grain:
                query.group_by(*(e.copy() for e in grain), copy=False)
            if predicate is not None:
                query.where(predicate, copy=False)
            if not query.selects:
                query.select(exp.Literal.number(1), copy=False).distinct(copy=False)

            # Metric aggregates use canonical model aliases; explicit dimension aliases
            # must also be honored inside conditional aggregates.
            aggregate_aliases = {
                remove_namespace(model): alias for model, alias in source_aliases.items()
            }
            for agg in aggs:
                for column in find_all_in_scope(agg, exp.Column):
                    if column.table in aggregate_aliases:
                        column.set("table", exp.to_identifier(aggregate_aliases[column.table]))

            outer_alias = f"__metric_source_{i}"
            keys: t.List[exp.Expr] = [exp.column(key, table=outer_alias) for key in group_names]
            if i == 0:
                select.from_(query.subquery(outer_alias, copy=False), copy=False)
                merged_keys = keys
            else:
                select.join(
                    query,
                    on=self._group_join_condition(merged_keys, keys),
                    join_type=self.join_type,
                    join_alias=outer_alias,
                    copy=False,
                )
                merged_keys = [
                    exp.Coalesce(this=left, expressions=[right])
                    for left, right in zip(merged_keys, keys)
                ]

        replacements = dict(zip(group_by, merged_keys))

        def replace_grain(node: exp.Expr) -> exp.Expr:
            if node in replacements:
                return replacements[node].copy()
            # Scalar subqueries belong to a separate SQL scope.
            return node.copy() if isinstance(node, exp.Query) else node

        for projection in select.selects:
            output_name = projection.output_name
            rewritten = projection.transform(replace_grain)
            if output_name and rewritten.output_name != output_name:
                rewritten = exp.alias_(rewritten, output_name, copy=False)
            projection.replace(rewritten)
        for clause in ("order", "having", "qualify", "distinct"):
            if select.args.get(clause) is not None:
                select.set(clause, select.args[clause].transform(replace_grain))
        if select.args.get("windows"):
            select.set(
                "windows", [window.transform(replace_grain) for window in select.args["windows"]]
            )

    def _resolve_columns(
        self,
        expression: exp.Expr,
        source: str,
        logical_alias: str,
        aliases: t.Dict[str, str],
        source_aliases: t.Dict[str, str],
        joins: t.Dict[str, t.Optional[exp.Join]],
    ) -> exp.Expr:
        for column in find_all_in_scope(expression, exp.Column):
            try:
                models = self.graph.models_for_column(source, column.name)
            except KeyError:
                models = []

            target = aliases.get(column.table)
            if target is not None:
                if target not in models:
                    raise ConfigError(
                        f"Cannot resolve metric dimension '{column}' from '{source}' via '{target}'"
                    )
            elif column.table and column.table != logical_alias:
                raise ConfigError(f"Unknown metric dimension alias '{column.table}' in '{column}'")
            elif source in models:
                target = source
            elif len(models) == 1:
                target = models[0]
            elif len(models) > 1:
                raise ConfigError(
                    f"Ambiguous metric dimension '{column}' from '{source}': {', '.join(models)}"
                )
            else:
                raise ConfigError(f"Cannot resolve metric dimension '{column}' from '{source}'")

            if target != source:
                joins.setdefault(target, None)
            column.set(
                "table", exp.to_identifier(source_aliases.get(target, remove_namespace(target)))
            )
        return expression

    def _add_joins(
        self,
        source: exp.Select,
        name: str,
        joins: t.Dict[str, t.Optional[exp.Join]],
        aliases: t.Dict[str, str],
    ) -> None:
        joined = {name}
        for target in joins:
            if target in joined:
                continue
            path = self.graph.find_path(name, target)
            if (
                not path
                or path[0].model_name != name
                or path[-1].model_name != target
                or any(a.name != b.name for a, b in zip(path, path[1:]))
            ):
                raise ConfigError(f"Cannot safely join metric dimension '{target}' from '{name}'")
            for a_ref, b_ref in zip(path, path[1:]):
                if b_ref.model_name in joined:
                    continue
                a = a_ref.expression.copy()
                b = b_ref.expression.copy()
                if isinstance(a, exp.Alias):
                    a = a.this
                if isinstance(b, exp.Alias):
                    b = b.this
                for expression, model in ((a, a_ref.model_name), (b, b_ref.model_name)):
                    for column in expression.find_all(exp.Column):
                        column.set(
                            "table", exp.to_identifier(aliases.get(model, remove_namespace(model)))
                        )
                on: exp.Condition = a.eq(b)
                explicit = joins.get(b_ref.model_name)
                if explicit is not None:
                    join = explicit.copy()
                    if join.args.get("on") is not None and join.args["on"] != on:
                        on = exp.and_(on, join.args["on"])
                    join.set("on", on)
                    join.set("using", None)
                    source.append("joins", join)
                else:
                    source.join(
                        b_ref.model_name,
                        on=on,
                        join_type="LEFT",
                        join_alias=aliases.get(
                            b_ref.model_name, remove_namespace(b_ref.model_name)
                        ),
                        dialect=self.dialect,
                        copy=False,
                    )
                joined.add(b_ref.model_name)


def _prepare_metric_references(expression: exp.Expr) -> exp.Expr:
    # A METRIC argument names a metric, not a column in one of the query's tables.
    for ref in expression.find_all(d.MetricAgg):
        if not isinstance(ref.this, (exp.Column, exp.Identifier)):
            raise ConfigError(f"Invalid metric reference '{ref.this}'")
        ref.set("this", exp.to_identifier(ref.this.name))
    return expression


def rewrite(
    sql: str | exp.Expr,
    graph: ReferenceGraph,
    metrics: t.Dict[str, Metric],
    dialect: t.Optional[str] = "",
) -> exp.Expr:
    rewriter = Rewriter(graph=graph, metrics=metrics, dialect=dialect)
    expression = d.parse_one(sql, dialect=dialect) if isinstance(sql, str) else sql
    try:
        return optimize(
            expression,
            dialect=dialect,
            quote_identifiers=False,
            rules=(
                _prepare_metric_references,
                qualify,
                rewriter.rewrite,
                optimize_joins,
            ),
        )
    except OptimizeError as ex:
        if expression.find(d.MetricAgg) is None:
            raise
        raise ConfigError(f"Cannot resolve metric query: {ex}") from ex
