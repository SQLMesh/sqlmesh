from __future__ import annotations

import typing as t
from pathlib import Path

from sqlglot import exp
from sqlglot.helper import first

from sqlmesh.core import dialect as d
from sqlmesh.core.node import str_or_exp_to_str
from sqlmesh.utils import UniqueKeyDict
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import PydanticModel, ValidationInfo, field_validator, validation_data

MeasureAndDimTables = t.Tuple[str, t.Tuple[str, ...]]


def load_metric_ddl(
    expression: exp.Expr, dialect: t.Optional[str], path: Path = Path(), **kwargs: t.Any
) -> MetricMeta:
    """Returns a MetricMeta from raw Metric DDL."""
    if not isinstance(expression, d.Metric):
        _raise_metric_config_error(
            f"Only METRIC(...) statements are allowed. Found {expression.sql(pretty=True)}", path
        )

    metric = MetricMeta(
        **{
            "dialect": dialect,
            "description": (
                "\n".join(comment.strip() for comment in expression.comments)
                if expression.comments
                else None
            ),
            **{prop.name.lower(): prop.args.get("value") for prop in expression.expressions},
            **kwargs,
        }
    )

    metric._path = path

    return metric


def expand_metrics(metas: UniqueKeyDict[str, MetricMeta]) -> UniqueKeyDict[str, Metric]:
    """Resolves all metas into standalone metrics."""
    metrics: UniqueKeyDict[str, Metric] = UniqueKeyDict("metrics")

    for name, meta in metas.items():
        if name not in metrics:
            metrics[name] = meta.to_metric(metas, metrics)

    return metrics


def remove_namespace(expression: str | exp.Column) -> str:
    """Given a column or a string, rewrite table namespaces like catalog.db to catalog__db"""

    if not isinstance(expression, str):
        expression = first(
            ".".join(p.name for p in column.parts[:-1])
            for column in expression.find_all(exp.Column)
            if column.table
        )
    return expression.replace('"', "").replace(".", "__")


class MetricMeta(PydanticModel, frozen=True):
    """Raw metric definition without relationships or expansion of derived metrics."""

    name: str
    dialect: str
    expression: exp.Expr
    description: t.Optional[str] = None
    owner: t.Optional[str] = None

    _path: Path = Path()

    @field_validator("name", mode="before")
    @classmethod
    def _name_validator(cls, v: t.Any) -> str:
        return (cls._string_validator(v) or "").lower()

    @field_validator("dialect", "owner", "description", mode="before")
    @classmethod
    def _string_validator(cls, v: t.Any) -> t.Optional[str]:
        return str_or_exp_to_str(v)

    @field_validator("expression", mode="before")
    def _validate_expression(cls, v: t.Any, info: ValidationInfo) -> exp.Expr:
        if isinstance(v, str):
            dialect = validation_data(info).get("dialect")
            return d.parse_one(v, dialect=dialect)
        if isinstance(v, exp.Expr):
            return v
        return v

    def to_metric(
        self, metas: t.Dict[str, MetricMeta], metrics: UniqueKeyDict[str, Metric]
    ) -> Metric:
        """Converts a metric meta into a fully expanded and standalone metric."""
        metric_refs = {}
        agg_or_ref = False

        for node in self.expression.walk():
            if isinstance(node, exp.Alias):
                _raise_metric_config_error(
                    f"Alias found for metric '{self.name}' which is not allowed", self._path
                )
            elif isinstance(node, exp.AggFunc):
                agg_or_ref = True
            elif isinstance(node, exp.Column) and not node.table:
                agg_or_ref = True
                ref = node.sql(dialect=self.dialect)

                if ref not in metrics:
                    metrics[ref] = metas[ref].to_metric(metas, metrics)

                metric_refs[node] = metrics[ref]

        if not agg_or_ref:
            _raise_metric_config_error(
                f"Metric '{self.name}' missing an aggregation or metric ref", self._path
            )

        if metric_refs:
            expanded = self.expression.copy()
            for column in expanded.find_all(exp.Column):
                metric = metric_refs.get(column)

                if metric:
                    column.replace(metric.expanded.copy())
        else:
            expanded = exp.alias_(self.expression, self.name)

        metric = Metric(**self.dict(), expanded=expanded)
        metric._path = self._path
        return metric


class Metric(MetricMeta, frozen=True):
    expanded: exp.Expr

    def _aggregate_output_names(
        self,
        aggregates: t.Optional[t.List[exp.AggFunc]] = None,
        used_names: t.Optional[t.Set[str]] = None,
    ) -> t.List[str]:
        """Returns the output column name for each aggregate in the expanded metric."""
        nested_aggregate_names: t.Dict[exp.AggFunc, str] = {}
        output_names = []
        used_names = used_names if used_names is not None else set()

        if aggregates is None:
            aggregates = list(self.expanded.find_all(exp.AggFunc))

        for agg in aggregates:
            if isinstance(agg.parent, exp.Alias):
                output_names.append(agg.parent.alias)
                used_names.add(agg.parent.alias)
                continue

            if agg not in nested_aggregate_names:
                base_name = (
                    f"__sqlmesh_metric_{self.name.replace('.', '_')}__agg_"
                    f"{len(nested_aggregate_names)}"
                )
                output_name = base_name
                suffix = 1
                while output_name in used_names:
                    output_name = f"{base_name}_{suffix}"
                    suffix += 1
                nested_aggregate_names[agg] = output_name
                used_names.add(output_name)
            output_names.append(nested_aggregate_names[agg])

        return output_names

    @property
    def aggs(self) -> t.Dict[exp.AggFunc, MeasureAndDimTables]:
        """Returns a dictionary of aggregation to referenced tables.

        This method removes catalog and schema information from columns.
        """
        aggregates = list(self.expanded.find_all(exp.AggFunc))
        return self._aggs_with_output_names(self._aggregate_output_names(aggregates))

    def _aggs_with_output_names(
        self, output_names: t.List[str]
    ) -> t.Dict[exp.AggFunc, MeasureAndDimTables]:
        aggs: t.Dict[exp.AggFunc, MeasureAndDimTables] = {}
        aggregates = list(self.expanded.find_all(exp.AggFunc))
        for agg, output_name in zip(aggregates, output_names):
            projection = (
                agg.parent
                if isinstance(agg.parent, exp.Alias)
                else exp.alias_(agg.copy(), output_name)
            )
            aggs[
                t.cast(
                    exp.AggFunc,
                    projection.transform(
                        lambda node: (
                            exp.column(node.this, table=remove_namespace(node))
                            if isinstance(node, exp.Column) and node.table
                            else node
                        )
                    ),
                )
            ] = _get_measure_and_dim_tables(agg)

        return aggs

    @property
    def formula(self) -> exp.Expr:
        """Returns the post aggregation formula of a metric.

        For simple metrics it is just the metric name. For derived metrics,
        it consists of the operations of the derived metrics without aggregations.
        """
        return self._formula_with_output_names(self._aggregate_output_names())

    def _formula_with_output_names(self, output_names: t.List[str]) -> exp.Expr:
        formula = (
            self.expanded.this.copy()
            if isinstance(self.expanded, exp.Alias)
            else self.expanded.copy()
        )
        formula_aggregates = list(formula.find_all(exp.AggFunc))
        aggregate_output_names = {
            id(agg): output_name for agg, output_name in zip(formula_aggregates, output_names)
        }

        def _replace_metric_alias(node: exp.Expr) -> exp.Expr:
            if not isinstance(node, exp.Alias):
                return node

            if isinstance(node.this, exp.AggFunc):
                return exp.Column(this=exp.to_identifier(node.alias))
            if node.this.find(exp.AggFunc):
                return node.this
            return exp.Column(this=exp.to_identifier(node.alias))

        formula = formula.transform(_replace_metric_alias, copy=False)

        def _replace_aggregate(node: exp.Expr) -> exp.Expr:
            if not isinstance(node, exp.AggFunc):
                return node
            output_name = aggregate_output_names[id(node)]
            return exp.Column(this=exp.to_identifier(output_name))

        formula = formula.transform(_replace_aggregate, copy=False)
        return exp.alias_(
            formula,
            self.name,
            copy=False,
        )


def _raise_metric_config_error(msg: str, path: Path) -> None:
    raise ConfigError(f"{msg}. '{path}'")


def _get_measure_and_dim_tables(expression: exp.Expr) -> MeasureAndDimTables:
    """Finds all the table references in a metric definition.

    Additionally ensure than the first table returned is the 'measure' or numeric value being aggregated.
    """

    tables = {}
    measure_table = None

    def is_measure(node: exp.Expr) -> bool:
        parent = node.parent

        if isinstance(parent, exp.AggFunc) and node.arg_key == "this":
            return True
        if isinstance(parent, (exp.If, exp.Case)) and node.arg_key != "this":
            return is_measure(parent)
        if isinstance(parent, (exp.Binary, exp.Paren, exp.Distinct)):
            return is_measure(parent)
        # AGG(x ORDER BY y) is parsed as AGG(Order(this=x, expressions=[Ordered(y)])).
        # Only the Order's `this` can be the measure, not the ORDER BY expressions.
        if isinstance(parent, exp.Order) and node.arg_key == "this":
            return is_measure(parent)
        return False

    for node in expression.walk():
        if isinstance(node, exp.Column) and node.table:
            table = ".".join(p.sql() for p in node.parts[:-1])
            tables[table] = True

            if not measure_table and is_measure(node):
                measure_table = table

    if not measure_table:
        raise ConfigError(f"Could not infer a measures table from '{expression}'")

    tables.pop(measure_table)
    return (measure_table, tuple(tables.keys()))
