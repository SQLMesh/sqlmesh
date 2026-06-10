from __future__ import annotations

import typing as t
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlglot import exp

from sqlmesh.core.context import Context
from sqlmesh.core.dialect import normalize_model_name
from sqlmesh.core.lineage import column_dependencies, lineage
from web.server.exceptions import ApiException
from web.server.models import LineageColumn
from web.server.settings import get_loaded_context

if t.TYPE_CHECKING:
    from sqlglot.lineage import Node


router = APIRouter()


def quote_column(column: str, dialect: str) -> str:
    return exp.to_identifier(column, quoted=True).sql(dialect=dialect)


def get_source_name(
    node: Node, default_catalog: t.Optional[str], dialect: str, model_name: str
) -> str:
    table = node.expression.find(exp.Table)
    if table:
        return normalize_model_name(table, default_catalog=default_catalog, dialect=dialect)
    if node.reference_node_name:
        # CTE name or derived table alias
        return f"{model_name}: {node.reference_node_name}"
    return ""


def get_column_name(node: Node) -> str:
    if isinstance(node.expression, exp.Alias):
        return node.expression.alias_or_name
    return exp.to_column(node.name).name


def create_lineage_adjacency_list(
    model_name: str, column_name: str, context: Context
) -> t.Dict[str, t.Dict[str, LineageColumn]]:
    """Create an adjacency list representation of a column's lineage graph including CTEs"""
    graph: t.Dict[str, t.Dict[str, LineageColumn]] = defaultdict(dict)
    visited = set()
    visited.add((model_name, column_name))
    nodes = [(model_name, column_name)]
    while nodes:
        model_name, column = nodes.pop(0)
        model = context.get_model(model_name)
        if not model:
            # External model
            graph[model_name][column] = LineageColumn(
                expression=f"FROM {model_name}",
                source=f"SELECT {column} FROM {model_name}",
                models={},
            )
            continue

        root = lineage(quote_column(column, model.dialect), model)

        for node in root.walk():
            if root.name == "UNION" and node is root:
                continue
            node_name = (
                get_source_name(
                    node,
                    default_catalog=context.default_catalog,
                    dialect=model.dialect,
                    model_name=model_name,
                )
                or model_name
            )
            node_column = get_column_name(node)
            if node_column in graph[node_name]:
                dependencies = defaultdict(set, graph[node_name][node_column].models)
            else:
                dependencies = defaultdict(set)
            for d in node.downstream:
                table = get_source_name(
                    d,
                    default_catalog=context.default_catalog,
                    dialect=model.dialect,
                    model_name=model_name,
                )
                if table:
                    column_name = get_column_name(d)
                    dependencies[table].add(column_name)
                    if isinstance(d.expression, exp.Table) and (table, column_name) not in visited:
                        nodes.append((table, column_name))
                        visited.add((table, column_name))

            graph[node_name][node_column] = LineageColumn(
                expression=node.expression.sql(pretty=True, dialect=model.dialect),
                source=node.source.sql(pretty=True, dialect=model.dialect),
                models=dependencies,
            )
    return graph


def create_models_only_lineage_adjacency_list(
    model_name: str, column_name: str, context: Context
) -> t.Dict[str, t.Dict[str, LineageColumn]]:
    """Create an adjacency list representation of a column's lineage graph only with models"""
    graph: t.Dict[str, t.Dict[str, LineageColumn]] = defaultdict(dict)
    nodes = [(model_name, column_name)]
    # visited is a set of tuples of (model_name, column_name) to prevent infinite recursion
    visited = set()
    visited.add((model_name, column_name))
    while nodes:
        model_name, column = nodes.pop(0)
        model = context.get_model(model_name)
        dependencies = defaultdict(set)
        if model:
            for table, column_names in column_dependencies(
                context, model_name, quote_column(column, model.dialect)
            ).items():
                for column_name in column_names:
                    if (table, column_name) not in visited:
                        dependencies[table].add(column_name)
                        nodes.append((table, column_name))
                        visited.add((table, column_name))

        graph[model_name][column] = LineageColumn(models=dependencies)
    return graph


@router.get("/{model_name:path}/columns/{column_name:str}")
def column_lineage(
    model_name: str,
    column_name: str,
    models_only: bool = False,
    context: Context = Depends(get_loaded_context),
) -> t.Dict[str, t.Dict[str, LineageColumn]]:
    """Get a column's lineage"""
    try:
        model_name = context.get_model(model_name).fqn
        if models_only:
            return create_models_only_lineage_adjacency_list(model_name, column_name, context)
        return create_lineage_adjacency_list(model_name, column_name, context)
    except Exception:
        raise ApiException(
            message="Unable to get column lineage",
            origin="API -> lineage -> column_lineage",
        )


@router.get("/{model_name:path}")
def model_lineage(
    model_name: str,
    context: Context = Depends(get_loaded_context),
) -> t.Dict[str, t.Set[str]]:
    """Get a model's lineage"""
    try:
        model_name = context.get_model(model_name).fqn
    except Exception:
        raise ApiException(
            message="Unable to get model lineage",
            origin="API -> lineage -> model_lineage",
        )

    return context.dag.lineage(model_name).graph
