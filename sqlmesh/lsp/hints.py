"""Type hinting on SQLMesh models"""

import typing as t

from lsprotocol import types

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlmesh.core.model.definition import SqlModel
from sqlmesh.lsp.context import LSPContext, ModelTarget
from sqlmesh.lsp.uri import URI


def get_hints(
    lsp_context: LSPContext,
    document_uri: URI,
    start_line: int,
    end_line: int,
) -> t.List[types.InlayHint]:
    """
    Get type hints for certain lines in a document

    Args:
        lint_context: The LSP context
        document_uri: The URI of the document
        start_line: the starting line to get hints for
        end_line: the ending line to get hints for

    Returns:
        A list of hints to apply to the document
    """
    path = document_uri.to_path()
    if path.suffix != ".sql":
        return []

    if path not in lsp_context.map:
        return []

    file_info = lsp_context.map[path]

    # Process based on whether it's a model or standalone audit
    if not isinstance(file_info, ModelTarget):
        return []

    # It's a model
    model = lsp_context.context.get_model(
        model_or_snapshot=file_info.names[0], raise_if_missing=False
    )
    if not isinstance(model, SqlModel):
        return []

    query = model.query
    dialect = model.dialect
    columns_to_types = model.columns_to_types or {}

    return _get_type_hints_for_model_from_query(
        query, dialect, columns_to_types, start_line, end_line
    )


def _get_type_hints_for_select(
    expression: exp.Expr,
    dialect: str,
    columns_to_types: t.Dict[str, exp.DataType],
    start_line: int,
    end_line: int,
) -> t.List[types.InlayHint]:
    hints: t.List[types.InlayHint] = []

    for select_exp in expression.expressions:
        if isinstance(select_exp, exp.Alias):
            if isinstance(select_exp.this, exp.Cast):
                continue

            meta = select_exp.args["alias"].meta

        elif isinstance(select_exp, exp.Column):
            meta = select_exp.parts[-1].meta
        else:
            continue

        if "line" not in meta or "col" not in meta:
            continue

        line = meta["line"]
        col = meta["col"]

        # Lines from sqlglot are 1 based
        line -= 1

        if line < start_line or line > end_line:
            continue

        name = select_exp.alias_or_name
        data_type = columns_to_types.get(name)

        if not data_type or data_type.is_type(exp.DataType.Type.UNKNOWN):
            continue

        type_label = data_type.sql(dialect)
        hints.append(
            types.InlayHint(
                label=f"::{type_label}",
                kind=types.InlayHintKind.Type,
                padding_left=False,
                padding_right=True,
                position=types.Position(line=line, character=col),
            )
        )

    return hints


def _get_type_hints_for_model_from_query(
    query: exp.Expr,
    dialect: str,
    columns_to_types: t.Dict[str, exp.DataType],
    start_line: int,
    end_line: int,
) -> t.List[types.InlayHint]:
    hints: t.List[types.InlayHint] = []
    try:
        query = normalize_identifiers(query.copy(), dialect=dialect)

        # Return the hints for top level selects (model definition columns only)
        return [
            hint
            for q in query.walk(prune=lambda n: not isinstance(n, exp.SetOperation))
            if isinstance(select := q.unnest(), exp.Select)
            for hint in _get_type_hints_for_select(
                q, dialect, columns_to_types, start_line, end_line
            )
        ]
    except Exception:
        return []
