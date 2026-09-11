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
        # Suspend each expression walk while resolving a dependency so cycles of
        # any depth can be diagnosed without using the Python call stack.
        stack: t.List[t.Tuple[MetricMeta, t.Iterator[exp.Expr], t.Dict[exp.Column, str], bool]] = [
            (self, self.expression.walk(), {}, False)
        ]
        visiting = {self.name}

        while True:
            meta, nodes, metric_refs, agg_or_ref = stack.pop()

            for node in nodes:
                if isinstance(node, exp.Alias):
                    _raise_metric_config_error(
                        f"Alias found for metric '{meta.name}' which is not allowed", meta._path
                    )
                elif isinstance(node, exp.AggFunc):
                    agg_or_ref = True
                elif isinstance(node, exp.Column) and not node.table:
                    agg_or_ref = True
                    ref = node.name.lower()
                    metric_refs[node] = ref

                    if ref not in metrics:
                        is_cycle = ref in visiting
                        if is_cycle or ref not in metas:
                            dependency_path = " -> ".join(
                                [frame[0].name for frame in stack] + [meta.name, ref]
                            )
                            if is_cycle:
                                message = f"Metric dependency cycle detected: {dependency_path}"
                            else:
                                message = (
                                    f"Unknown metric '{ref}' referenced by metric '{meta.name}' "
                                    f"(dependency path: {dependency_path})"
                                )
                            _raise_metric_config_error(message, meta._path)

                        dependency = metas[ref]
                        stack.append((meta, nodes, metric_refs, agg_or_ref))
                        stack.append((dependency, dependency.expression.walk(), {}, False))
                        visiting.add(ref)
                        break
            else:
                if not agg_or_ref:
                    _raise_metric_config_error(
                        f"Metric '{meta.name}' missing an aggregation or metric ref", meta._path
                    )

                if metric_refs:
                    expanded = meta.expression.copy()
                    for column in expanded.find_all(exp.Column):
                        reference = metric_refs.get(column)
                        if reference is not None:
                            column.replace(metrics[reference].expanded.copy())
                else:
                    expanded = exp.alias_(meta.expression, meta.name)

                metric = Metric(**meta.dict(), expanded=expanded)
                metric._path = meta._path
                visiting.remove(meta.name)

                if not stack:
                    return metric

                metrics[meta.name] = metric


class Metric(MetricMeta, frozen=True):
    expanded: exp.Expr

    @property
    def aggs(self) -> t.Dict[exp.AggFunc, MeasureAndDimTables]:
        """Returns a dictionary of aggregation to referenced tables.

        This method removes catalog and schema information from columns.
        """
        return {
            t.cast(
                exp.AggFunc,
                t.cast(exp.Expr, agg.parent).transform(
                    lambda node: (
                        exp.column(node.this, table=remove_namespace(node))
                        if isinstance(node, exp.Column) and node.table
                        else node
                    )
                ),
            ): _get_measure_and_dim_tables(agg)
            for agg in self.expanded.find_all(exp.AggFunc)
        }

    @property
    def formula(self) -> exp.Expr:
        """Returns the post aggregation formula of a metric.

        For simple metrics it is just the metric name. For derived metrics,
        it consists of the operations of the derived metrics without aggregations.
        """
        return exp.alias_(
            self.expanded.transform(
                lambda node: exp.column(node.args["alias"]) if isinstance(node, exp.Alias) else node
            ),
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
