"""Type hinting on SQLMesh models"""

import typing as t

from lsprotocol import types

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlmesh.core.dialect import parse
from sqlmesh.core.model.definition import SqlModel, _split_sql_model_statements
from sqlmesh.lsp.context import LSPContext, ModelTarget
from sqlmesh.lsp.uri import URI


def get_hints(
    lsp_context: LSPContext,
    document_uri: URI,
    start_line: int,
    end_line: int,
    document_text: t.Optional[str] = None,
) -> t.List[types.InlayHint]:
    """
    Get type hints for certain lines in a document

    Args:
        lint_context: The LSP context
        document_uri: The URI of the document
        start_line: the starting line to get hints for
        end_line: the ending line to get hints for
        document_text: the text currently held by the editor, if it may differ from the
            text the context was loaded from

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

    dialect = model.dialect
    columns_to_types = model.columns_to_types or {}

    query: exp.Expr
    if document_text is None:
        query = model.query
    else:
        # The context is only reloaded when the document is saved, so the model's query
        # carries the positions of the text as it was last saved. Placing hints at those
        # positions puts them inside tokens the user is still editing, so take the
        # positions from the text the editor currently holds instead. Column types are
        # still looked up by name on the loaded model, and a column that isn't on it yet
        # simply gets no hint until the next reload.
        parsed_query = _query_from_document_text(document_text, dialect)
        if parsed_query is None:
            return []
        query = parsed_query

    return _get_type_hints_for_model_from_query(
        query, dialect, columns_to_types, start_line, end_line
    )


def _query_from_document_text(document_text: str, dialect: str) -> t.Optional[exp.Expr]:
    """Extract the model's query from the raw text of a model file.

    Returns None if the text cannot be parsed, which is expected while the user is
    part-way through an edit.
    """
    try:
        expressions = parse(document_text, default_dialect=dialect)
        if not expressions:
            return None
        query, *_ = _split_sql_model_statements(expressions[1:], None, dialect=dialect)
        return query
    except Exception:
        return None


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
