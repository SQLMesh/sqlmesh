from __future__ import annotations

import functools
import logging
import re
import sys
import typing as t
from contextlib import contextmanager
from difflib import unified_diff
from enum import Enum, auto
from functools import lru_cache

from sqlglot import Dialect, Generator, ParseError, Parser, Tokenizer, TokenType, exp
from sqlglot.dialects.dialect import DialectType
from sqlglot.dialects import DuckDB, Snowflake
import sqlglot.dialects.athena as athena
from sqlglot.helper import seq_get
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify_columns import quote_identifiers
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema
from sqlglot.tokens import Token

from sqlmesh.core.constants import MAX_MODEL_DEFINITION_SIZE
from sqlmesh.utils import get_source_columns_to_types
from sqlmesh.utils.errors import SQLMeshError, ConfigError
from sqlmesh.utils.pandas import columns_to_types_from_df

if t.TYPE_CHECKING:
    import pandas as pd

    from sqlglot._typing import E


SQLMESH_MACRO_PREFIX = "@"

TABLES_META = "sqlmesh.tables"

logger = logging.getLogger(__name__)


class Model(exp.Expression):
    arg_types = {"expressions": True}


class Audit(exp.Expression):
    arg_types = {"expressions": True}


class Metric(exp.Expression):
    arg_types = {"expressions": True}


class Jinja(exp.Func):
    arg_types = {"this": True}


class JinjaQuery(Jinja):
    pass


class JinjaStatement(Jinja):
    pass


class VirtualUpdateStatement(exp.Expression):
    arg_types = {"expressions": True}


class ModelKind(exp.Expression):
    arg_types = {"this": True, "expressions": False}


class MacroVar(exp.Var):
    pass


class MacroFunc(exp.Func):
    @property
    def name(self) -> str:
        return self.this.name


class MacroDef(MacroFunc):
    arg_types = {"this": True, "expression": True}


class MacroSQL(MacroFunc):
    arg_types = {"this": True, "into": False}


class MacroStrReplace(MacroFunc):
    pass


class PythonCode(exp.Expression):
    arg_types = {"expressions": True}


class DColonCast(exp.Cast):
    pass


class MetricAgg(exp.AggFunc):
    """Used for computing metrics."""

    arg_types = {"this": True}

    @property
    def output_name(self) -> str:
        return self.this.name


class StagedFilePath(exp.Expression):
    """Represents paths to "staged files" in Snowflake."""

    arg_types = exp.Table.arg_types.copy()


def _parse_statement(self: Parser) -> t.Optional[exp.Expression]:
    if self._curr is None:
        return None

    parser = PARSERS.get(self._curr.text.upper())
    error_msg = None

    if parser:
        # Capture any available description in the form of a comment
        comments = self._curr.comments

        index = self._index
        try:
            self._advance()
            meta = self._parse_wrapped(lambda: t.cast(t.Callable, parser)(self))
        except ParseError as parse_error:
            error_msg = parse_error.args[0]
            self._retreat(index)

        # Only return the DDL expression if we actually managed to parse one. This is
        # done in order to allow parsing standalone identifiers / function calls like
        # "metric", or "model(1, 2, 3)", which collide with SQLMesh's DDL syntax.
        if self._index != index:
            meta.comments = comments
            return meta

    try:
        return self.__parse_statement()  # type: ignore
    except ParseError:
        if error_msg:
            raise ParseError(error_msg)
        raise


def _parse_lambda(self: Parser, alias: bool = False) -> t.Optional[exp.Expression]:
    node = self.__parse_lambda(alias=alias)  # type: ignore
    if isinstance(node, exp.Lambda):
        node.set("this", self._parse_alias(node.this))
    return node


def _parse_id_var(
    self: Parser,
    any_token: bool = True,
    tokens: t.Optional[t.Collection[TokenType]] = None,
) -> t.Optional[exp.Expression]:
    if self._prev and self._prev.text == SQLMESH_MACRO_PREFIX and self._match(TokenType.L_BRACE):
        identifier = self.__parse_id_var(any_token=any_token, tokens=tokens)  # type: ignore
        if not self._match(TokenType.R_BRACE):
            self.raise_error("Expecting }")
        identifier.args["this"] = f"@{{{identifier.name}}}"
    else:
        identifier = self.__parse_id_var(any_token=any_token, tokens=tokens)  # type: ignore

    while (
        identifier
        and not identifier.args.get("quoted")
        and self._is_connected()
        and (
            self._match_texts(("{", SQLMESH_MACRO_PREFIX))
            or self._curr.token_type not in self.RESERVED_TOKENS
        )
    ):
        this = identifier.name
        brace = False

        if self._prev.text == "{":
            this += "{"
            brace = True
        else:
            if self._prev.text == SQLMESH_MACRO_PREFIX:
                this += "@"
            if self._match(TokenType.L_BRACE):
                this += "{"
                brace = True

        next_id = self._parse_id_var(any_token=False)

        if next_id:
            this += next_id.name
        else:
            return identifier

        if brace:
            if self._match(TokenType.R_BRACE):
                this += "}"
            else:
                self.raise_error("Expecting }")

        identifier = self.expression(exp.Identifier, this=this, quoted=identifier.quoted)

    return identifier


def _parse_macro(self: Parser, keyword_macro: str = "") -> t.Optional[exp.Expression]:
    if self._prev.text != SQLMESH_MACRO_PREFIX:
        return self._parse_parameter()

    comments = self._prev.comments
    index = self._index
    field = self._parse_primary() or self._parse_function(functions={}) or self._parse_id_var()

    def _build_macro(field: t.Optional[exp.Expression]) -> t.Optional[exp.Expression]:
        if isinstance(field, exp.Func):
            macro_name = field.name.upper()
            if macro_name != keyword_macro and macro_name in KEYWORD_MACROS:
                self._retreat(index)
                return None

            if isinstance(field, exp.Anonymous):
                if macro_name == "DEF":
                    return self.expression(
                        MacroDef,
                        this=field.expressions[0],
                        expression=field.expressions[1],
                        comments=comments,
                    )
                if macro_name == "SQL":
                    into = field.expressions[1].this.lower() if len(field.expressions) > 1 else None
                    return self.expression(
                        MacroSQL, this=field.expressions[0], into=into, comments=comments
                    )
            else:
                field = self.expression(
                    exp.Anonymous,
                    this=field.sql_name(),
                    expressions=list(field.args.values()),
                    comments=comments,
                )

            return self.expression(MacroFunc, this=field, comments=comments)

        if field is None:
            return None

        if field.is_string or (isinstance(field, exp.Identifier) and field.quoted):
            return self.expression(
                MacroStrReplace, this=exp.Literal.string(field.this), comments=comments
            )

        if "@" in field.this:
            return field
        return self.expression(MacroVar, this=field.this, comments=comments)

    if isinstance(field, (exp.Window, exp.IgnoreNulls, exp.RespectNulls)):
        field.set("this", _build_macro(field.this))
    else:
        field = _build_macro(field)

    return field


KEYWORD_MACROS = {"WITH", "JOIN", "WHERE", "GROUP_BY", "HAVING", "ORDER_BY", "LIMIT"}


def _parse_matching_macro(self: Parser, name: str) -> t.Optional[exp.Expression]:
    if not self._match_pair(TokenType.PARAMETER, TokenType.VAR, advance=False) or (
        self._next and self._next.text.upper() != name.upper()
    ):
        return None

    self._advance()
    return _parse_macro(self, keyword_macro=name)


def _parse_body_macro(self: Parser) -> t.Tuple[str, t.Optional[exp.Expression]]:
    name = self._next and self._next.text.upper()

    if name == "JOIN":
        return ("joins", self._parse_join())
    if name == "WHERE":
        return ("where", self._parse_where())
    if name == "GROUP_BY":
        return ("group", self._parse_group())
    if name == "HAVING":
        return ("having", self._parse_having())
    if name == "ORDER_BY":
        return ("order", self._parse_order())
    if name == "LIMIT":
        return ("limit", self._parse_limit())
    return ("", None)


def _parse_with(self: Parser, skip_with_token: bool = False) -> t.Optional[exp.Expression]:
    macro = _parse_matching_macro(self, "WITH")
    if not macro:
        return self.__parse_with(skip_with_token=skip_with_token)  # type: ignore

    macro.this.append("expressions", self.__parse_with(skip_with_token=True))  # type: ignore
    return macro


def _parse_join(
    self: Parser, skip_join_token: bool = False, parse_bracket: bool = False
) -> t.Optional[exp.Expression]:
    index = self._index
    method, side, kind = self._parse_join_parts()
    macro = _parse_matching_macro(self, "JOIN")
    if not macro:
        self._retreat(index)
        return self.__parse_join(skip_join_token=skip_join_token, parse_bracket=parse_bracket)  # type: ignore

    join = self.__parse_join(skip_join_token=True)  # type: ignore
    if method:
        join.set("method", method.text)
    if side:
        join.set("side", side.text)
    if kind:
        join.set("kind", kind.text)

    macro.this.append("expressions", join)
    return macro


def _warn_unsupported(self: Parser) -> None:
    from sqlmesh.core.console import get_console

    sql = self._find_sql(self._tokens[0], self._tokens[-1])[: self.error_message_context]

    get_console().log_warning(
        f"'{sql}' could not be semantically understood as it contains unsupported syntax, SQLMesh will treat the command as is. Note that any references to the model's "
        "underlying physical table can't be resolved in this case, consider using Jinja as explained here https://sqlmesh.readthedocs.io/en/stable/concepts/macros/macro_variables/#audit-only-variables"
    )


def _parse_select(
    self: Parser,
    nested: bool = False,
    table: bool = False,
    parse_subquery_alias: bool = True,
    parse_set_operation: bool = True,
    consume_pipe: bool = True,
    from_: t.Optional[exp.From] = None,
) -> t.Optional[exp.Expression]:
    select = self.__parse_select(  # type: ignore
        nested=nested,
        table=table,
        parse_subquery_alias=parse_subquery_alias,
        parse_set_operation=parse_set_operation,
        consume_pipe=consume_pipe,
        from_=from_,
    )

    if (
        not select
        and not parse_set_operation
        and self._match_pair(TokenType.PARAMETER, TokenType.VAR, advance=False)
    ):
        self._advance()
        return _parse_macro(self)

    return select


def _parse_where(self: Parser, skip_where_token: bool = False) -> t.Optional[exp.Expression]:
    macro = _parse_matching_macro(self, "WHERE")
    if not macro:
        return self.__parse_where(skip_where_token=skip_where_token)  # type: ignore

    macro.this.append("expressions", self.__parse_where(skip_where_token=True))  # type: ignore
    return macro


def _parse_group(self: Parser, skip_group_by_token: bool = False) -> t.Optional[exp.Expression]:
    macro = _parse_matching_macro(self, "GROUP_BY")
    if not macro:
        return self.__parse_group(skip_group_by_token=skip_group_by_token)  # type: ignore

    macro.this.append("expressions", self.__parse_group(skip_group_by_token=True))  # type: ignore
    return macro


def _parse_having(self: Parser, skip_having_token: bool = False) -> t.Optional[exp.Expression]:
    macro = _parse_matching_macro(self, "HAVING")
    if not macro:
        return self.__parse_having(skip_having_token=skip_having_token)  # type: ignore

    macro.this.append("expressions", self.__parse_having(skip_having_token=True))  # type: ignore
    return macro


def _parse_order(
    self: Parser, this: t.Optional[exp.Expression] = None, skip_order_token: bool = False
) -> t.Optional[exp.Expression]:
    macro = _parse_matching_macro(self, "ORDER_BY")
    if not macro:
        return self.__parse_order(this, skip_order_token=skip_order_token)  # type: ignore

    macro.this.append("expressions", self.__parse_order(this, skip_order_token=True))  # type: ignore
    return macro


def _parse_limit(
    self: Parser,
    this: t.Optional[exp.Expression] = None,
    top: bool = False,
    skip_limit_token: bool = False,
) -> t.Optional[exp.Expression]:
    macro = _parse_matching_macro(self, "TOP" if top else "LIMIT")
    if not macro:
        return self.__parse_limit(this, top=top, skip_limit_token=skip_limit_token)  # type: ignore

    macro.this.append("expressions", self.__parse_limit(this, top=top, skip_limit_token=True))  # type: ignore
    return macro


def _parse_value(self: Parser, values: bool = True) -> t.Optional[exp.Expression]:
    wrapped = self._match(TokenType.L_PAREN, advance=False)

    # The base _parse_value method always constructs a Tuple instance. This is problematic when
    # generating values with a macro function, because it's impossible to tell whether the user's
    # intention was to construct a row or a column with the VALUES expression. To avoid this, we
    # amend the AST such that the Tuple is replaced by the macro function call itself.
    expr = self.__parse_value()  # type: ignore
    if expr and not wrapped and isinstance(seq_get(expr.expressions, 0), MacroFunc):
        return expr.expressions[0]

    return expr


def _parse_macro_or_clause(self: Parser, parser: t.Callable) -> t.Optional[exp.Expression]:
    return _parse_macro(self) if self._match(TokenType.PARAMETER) else parser()


def _parse_props(self: Parser) -> t.Optional[exp.Expression]:
    key = self._parse_id_var(any_token=True)
    if not key:
        return None

    name = key.name.lower()
    if name == "time_data_type":
        # TODO: if we make *_data_type a convention to parse things into exp.DataType, we could make this more generic
        value = self._parse_types(schema=True)
    elif name == "when_matched":
        # Parentheses around the WHEN clauses can be used to disambiguate them from other properties
        value = self._parse_wrapped(
            lambda: _parse_macro_or_clause(self, self._parse_when_matched),
            optional=True,
        )
    elif name == "merge_filter":
        value = self._parse_conjunction()
    elif self._match(TokenType.L_PAREN):
        value = self.expression(exp.Tuple, expressions=self._parse_csv(self._parse_equality))
        self._match_r_paren()
    else:
        value = self._parse_bracket(self._parse_field(any_token=True))

    if name == "path" and value:
        # Make sure if we get a windows path that it is converted to posix
        value = exp.Literal.string(value.this.replace("\\", "/"))  # type: ignore

    return self.expression(exp.Property, this=name, value=value)


def _parse_types(
    self: Parser,
    check_func: bool = False,
    schema: bool = False,
    allow_identifiers: bool = True,
) -> t.Optional[exp.Expression]:
    start = self._curr
    parsed_type = self.__parse_types(  # type: ignore
        check_func=check_func, schema=schema, allow_identifiers=allow_identifiers
    )

    if schema and parsed_type:
        parsed_type.meta["sql"] = self._find_sql(start, self._prev)

    return parsed_type


# Only needed for Snowflake: its "staged file" syntax (@<path>) clashes with our macro
# var syntax. By converting the Var representation to a MacroVar, we should be able to
# handle both use cases: if there's no value in the MacroEvaluator's context for that
# MacroVar, it'll render into @<path>, so it won't break staged file path references.
#
# See: https://docs.snowflake.com/en/user-guide/querying-stage
def _parse_table_parts(
    self: Parser, schema: bool = False, is_db_reference: bool = False, wildcard: bool = False
) -> exp.Table | StagedFilePath:
    index = self._index
    table = self.__parse_table_parts(  # type: ignore
        schema=schema, is_db_reference=is_db_reference, wildcard=wildcard
    )

    table_arg = table.this
    name = table_arg.name if isinstance(table_arg, exp.Var) else ""

    if name.startswith(SQLMESH_MACRO_PREFIX):
        # In these cases, we don't want to produce a `StagedFilePath` node:
        #
        # - @'...' needs to parsed as a string template
        # - @{foo}.bar needs to be parsed as a table with a macro var part
        # - @name(arg1 [, arg2 ...]) needs to be parsed as a macro function call
        #
        # These cases can unambiguously be parsed using the base `_parse_table_parts`, as there
        # is no overlap with staged files https://docs.snowflake.com/en/user-guide/querying-stage
        if (
            self._prev.token_type == TokenType.STRING
            or "{" in name
            or (
                self._curr
                and self._prev.token_type in (TokenType.L_PAREN, TokenType.R_PAREN)
                and self._curr.text.upper() not in ("FILE_FORMAT", "PATTERN")
                and not (table.args.get("format") or table.args.get("pattern"))
            )
        ):
            self._retreat(index)
            return Parser._parse_table_parts(self, schema=schema, is_db_reference=is_db_reference)

        table_arg.replace(MacroVar(this=name[1:]))
        return StagedFilePath(**table.args)

    return table


def _parse_if(self: Parser) -> t.Optional[exp.Expression]:
    # If we fail to parse an IF function with expressions as arguments, we then try
    # to parse a statement / command to support the macro @IF(condition, statement)
    index = self._index
    try:
        return self.__parse_if()  # type: ignore
    except ParseError:
        self._retreat(index)
        self._match_l_paren()

        cond = self._parse_conjunction()
        self._match(TokenType.COMMA)

        # Try to parse a known statement, otherwise fall back to parsing a command
        # Since the trailing `)` token is not expected by the statement parsers, we
        # remove it from the token stream before trying to parse the statement.
        last_token = self._tokens[-1]
        if last_token.token_type == TokenType.R_PAREN:
            self._tokens[-2].comments.extend(last_token.comments)
            self._tokens.pop()
        else:
            self.raise_error("Expecting )")

        index = self._index
        stmt = self._parse_statement()
        if self._curr:
            self._retreat(index)
            stmt = self._parse_as_command(self._tokens[index])

        return exp.Anonymous(this="IF", expressions=[cond, stmt])


def _create_parser(expression_type: t.Type[exp.Expression], table_keys: t.List[str]) -> t.Callable:
    def parse(self: Parser) -> t.Optional[exp.Expression]:
        from sqlmesh.core.model.kind import ModelKindName

        expressions: t.List[exp.Expression] = []

        while True:
            prev_property = seq_get(expressions, -1)
            if not self._match(TokenType.COMMA, expression=prev_property) and expressions:
                break

            key_expression = self._parse_id_var(any_token=True)
            if not key_expression:
                break

            # This allows macro functions that programmaticaly generate the property key-value pair
            if isinstance(key_expression, MacroFunc):
                expressions.append(key_expression)
                continue

            key = key_expression.name.lower()

            start = self._curr
            value: t.Optional[exp.Expression | str]

            if key in table_keys:
                value = self._parse_table_parts()
                if value and self._prev.token_type == TokenType.STRING:
                    self.raise_error(
                        f"'{key}' property cannot be a string value: {value}. "
                        "Please use the identifier syntax instead, e.g. foo.bar instead of 'foo.bar'"
                    )
            elif key == "columns":
                value = self._parse_schema()
            elif key == "kind":
                field = _parse_macro_or_clause(self, lambda: self._parse_id_var(any_token=True))

                if not field or isinstance(field, (MacroVar, MacroFunc)):
                    value = field
                else:
                    try:
                        kind = ModelKindName[field.name.upper()]
                    except KeyError:
                        raise SQLMeshError(
                            f"Model kind specified as '{field.name}', but that is not a valid model kind.\n\nPlease specify one of {', '.join(ModelKindName)}."
                        )

                    if kind in (
                        ModelKindName.INCREMENTAL_BY_TIME_RANGE,
                        ModelKindName.INCREMENTAL_BY_UNIQUE_KEY,
                        ModelKindName.INCREMENTAL_BY_PARTITION,
                        ModelKindName.INCREMENTAL_UNMANAGED,
                        ModelKindName.SEED,
                        ModelKindName.VIEW,
                        ModelKindName.SCD_TYPE_2,
                        ModelKindName.SCD_TYPE_2_BY_TIME,
                        ModelKindName.SCD_TYPE_2_BY_COLUMN,
                        ModelKindName.CUSTOM,
                    ) and self._match(TokenType.L_PAREN, advance=False):
                        props = self._parse_wrapped_csv(functools.partial(_parse_props, self))
                    else:
                        props = None

                    value = self.expression(ModelKind, this=kind.value, expressions=props)
            elif key == "expression":
                value = self._parse_conjunction()
            elif key == "partitioned_by":
                partitioned_by = self._parse_partitioned_by()
                if isinstance(partitioned_by.this, exp.Schema):
                    value = exp.tuple_(*partitioned_by.this.expressions)
                else:
                    value = partitioned_by.this
            else:
                value = self._parse_bracket(self._parse_field(any_token=True))

            if isinstance(value, exp.Expression):
                value.meta["sql"] = self._find_sql(start, self._prev)

            expressions.append(self.expression(exp.Property, this=key, value=value))

        return self.expression(expression_type, expressions=expressions)

    return parse


PARSERS = {
    "MODEL": _create_parser(Model, ["name"]),
    "AUDIT": _create_parser(Audit, ["model"]),
    "METRIC": _create_parser(Metric, ["name"]),
}


def _props_sql(self: Generator, expressions: t.List[exp.Expression]) -> str:
    props = []
    size = len(expressions)

    for i, prop in enumerate(expressions):
        if isinstance(prop, MacroFunc):
            sql = self.indent(self.sql(prop, comment=False))
        else:
            sql = self.indent(f"{prop.name} {self.sql(prop, 'value')}")

        if i < size - 1:
            sql += ","

        props.append(self.maybe_comment(sql, expression=prop))

    return "\n".join(props)


def _on_virtual_update_sql(self: Generator, expressions: t.List[exp.Expression]) -> str:
    statements = "\n".join(
        self.sql(expression)
        if isinstance(expression, JinjaStatement)
        else f"{self.sql(expression)};"
        for expression in expressions
    )
    return f"{ON_VIRTUAL_UPDATE_BEGIN};\n{statements}\n{ON_VIRTUAL_UPDATE_END};"


def _sqlmesh_ddl_sql(self: Generator, expression: Model | Audit | Metric, name: str) -> str:
    return "\n".join([f"{name} (", _props_sql(self, expression.expressions), ")"])


def _model_kind_sql(self: Generator, expression: ModelKind) -> str:
    props = _props_sql(self, expression.expressions)
    if props:
        return "\n".join([f"{expression.this} (", props, ")"])
    return expression.name.upper()


def _macro_keyword_func_sql(self: Generator, expression: exp.Expression) -> str:
    name = expression.name
    keyword = name.replace("_", " ")
    *args, clause = expression.expressions
    macro = f"@{name}({self.format_args(*args)})"
    return self.sql(clause).replace(keyword, macro, 1)


def _macro_func_sql(self: Generator, expression: MacroFunc) -> str:
    expression = expression.this
    name = expression.name
    if name in KEYWORD_MACROS:
        sql = _macro_keyword_func_sql(self, expression)
    else:
        sql = f"@{name}({self.format_args(*expression.expressions)})"
    return self.maybe_comment(sql, expression)


def _whens_sql(self: Generator, expression: exp.Whens) -> str:
    if isinstance(expression.parent, exp.Merge):
        return self.whens_sql(expression)

    # If the `WHEN` clauses aren't part of a MERGE statement (e.g. they
    # appear in the `MODEL` DDL), then we will wrap them with parentheses.
    return self.wrap(self.expressions(expression, sep=" ", indent=False))


def _override(klass: t.Type[Tokenizer | Parser], func: t.Callable) -> None:
    name = func.__name__
    setattr(klass, f"_{name}", getattr(klass, name))
    setattr(klass, name, func)


def format_model_expressions(
    expressions: t.List[exp.Expression],
    dialect: t.Optional[str] = None,
    rewrite_casts: bool = True,
    **kwargs: t.Any,
) -> str:
    """Format a model's expressions into a standardized format.

    Args:
        expressions: The model's expressions, must be at least model def + query.
        dialect: The dialect to render the expressions as.
        rewrite_casts: Whether to rewrite all casts to use the :: syntax.
        **kwargs: Additional keyword arguments to pass to the sql generator.

    Returns:
        A string representing the formatted model.
    """
    if len(expressions) == 1 and is_meta_expression(expressions[0]):
        return expressions[0].sql(pretty=True, dialect=dialect)

    if rewrite_casts:

        def cast_to_colon(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Cast) and not any(
                # Only convert CAST into :: if it doesn't have additional args set, otherwise this
                # conversion could alter the semantics (eg. changing SAFE_CAST in BigQuery to CAST)
                arg
                for name, arg in node.args.items()
                if name not in ("this", "to")
            ):
                this = node.this

                if not isinstance(this, (exp.Binary, exp.Unary)) or isinstance(this, exp.Paren):
                    cast = DColonCast(this=this, to=node.to)
                    cast.comments = node.comments
                    node = cast

            exp.replace_children(node, cast_to_colon)
            return node

        new_expressions = []
        for expression in expressions:
            expression = expression.copy()
            exp.replace_children(expression, cast_to_colon)
            new_expressions.append(expression)

        expressions = new_expressions

    return ";\n\n".join(
        expression.sql(pretty=True, dialect=dialect, **kwargs) for expression in expressions
    ).strip()


def text_diff(
    a: t.List[exp.Expression],
    b: t.List[exp.Expression],
    a_dialect: t.Optional[str] = None,
    b_dialect: t.Optional[str] = None,
) -> str:
    """Find the unified text diff between two expressions."""
    a_sql = [
        line
        for expr in a
        for line in expr.sql(pretty=True, comments=False, dialect=a_dialect).split("\n")
    ]
    b_sql = [
        line
        for expr in b
        for line in expr.sql(pretty=True, comments=False, dialect=b_dialect).split("\n")
    ]
    return "\n".join(unified_diff(a_sql, b_sql))


WS_OR_COMMENT = r"(?:\s|--[^\n]*\n|/\*.*?\*/)"
HEADER = r"\b(?:model|audit)\b(?=\s*\()"
KEY_BOUNDARY = r"(?:\(|,)"  # key is preceded by either '(' or ','
DIALECT_VALUE = r"['\"]?(?P<dialect>[a-z][a-z0-9]*)['\"]?"
VALUE_BOUNDARY = r"(?=,|\))"  # value is followed by comma or closing paren

DIALECT_PATTERN = re.compile(
    rf"{HEADER}.*?{KEY_BOUNDARY}{WS_OR_COMMENT}*dialect{WS_OR_COMMENT}+{DIALECT_VALUE}{WS_OR_COMMENT}*{VALUE_BOUNDARY}",
    re.IGNORECASE | re.DOTALL,
)


def _is_command_statement(command: str, tokens: t.List[Token], pos: int) -> bool:
    try:
        return (
            tokens[pos].text.upper() == command.upper()
            and tokens[pos + 1].token_type == TokenType.SEMICOLON
        )
    except IndexError:
        return False


JINJA_QUERY_BEGIN = "JINJA_QUERY_BEGIN"
JINJA_STATEMENT_BEGIN = "JINJA_STATEMENT_BEGIN"
JINJA_END = "JINJA_END"
ON_VIRTUAL_UPDATE_BEGIN = "ON_VIRTUAL_UPDATE_BEGIN"
ON_VIRTUAL_UPDATE_END = "ON_VIRTUAL_UPDATE_END"


def _is_jinja_statement_begin(tokens: t.List[Token], pos: int) -> bool:
    return _is_command_statement(JINJA_STATEMENT_BEGIN, tokens, pos)


def _is_jinja_query_begin(tokens: t.List[Token], pos: int) -> bool:
    return _is_command_statement(JINJA_QUERY_BEGIN, tokens, pos)


def _is_jinja_end(tokens: t.List[Token], pos: int) -> bool:
    return _is_command_statement(JINJA_END, tokens, pos)


def jinja_query(query: str) -> JinjaQuery:
    return JinjaQuery(this=exp.Literal.string(query.strip()))


def jinja_statement(statement: str) -> JinjaStatement:
    return JinjaStatement(this=exp.Literal.string(statement.strip()))


def _is_virtual_statement_begin(tokens: t.List[Token], pos: int) -> bool:
    return _is_command_statement(ON_VIRTUAL_UPDATE_BEGIN, tokens, pos)


def _is_virtual_statement_end(tokens: t.List[Token], pos: int) -> bool:
    return _is_command_statement(ON_VIRTUAL_UPDATE_END, tokens, pos)


def virtual_statement(statements: t.List[exp.Expression]) -> VirtualUpdateStatement:
    return VirtualUpdateStatement(expressions=statements)


class ChunkType(Enum):
    JINJA_QUERY = auto()
    JINJA_STATEMENT = auto()
    SQL = auto()
    VIRTUAL_STATEMENT = auto()
    VIRTUAL_JINJA_STATEMENT = auto()


def parse_one(
    sql: str, dialect: t.Optional[str] = None, into: t.Optional[exp.IntoType] = None
) -> exp.Expression:
    expressions = parse(sql, default_dialect=dialect, match_dialect=False, into=into)
    if not expressions:
        raise SQLMeshError(f"No expressions found in '{sql}'")
    elif len(expressions) > 1:
        raise SQLMeshError(f"Multiple expressions found in '{sql}'")
    return expressions[0]


def parse(
    sql: str,
    default_dialect: t.Optional[str] = None,
    match_dialect: bool = True,
    into: t.Optional[exp.IntoType] = None,
) -> t.List[exp.Expression]:
    """Parse a sql string.

    Supports parsing model definition.
    If a jinja block is detected, the query is stored as raw string in a Jinja node.

    Args:
        sql: The sql based definition.
        default_dialect: The dialect to use if the model does not specify one.

    Returns:
        A list of the parsed expressions: [Model, *Statements, Query, *Statements]
    """
    match = match_dialect and DIALECT_PATTERN.search(sql[:MAX_MODEL_DEFINITION_SIZE])
    dialect_str = match.group("dialect") if match else None
    dialect = Dialect.get_or_raise(dialect_str or default_dialect)

    tokens = dialect.tokenize(sql)
    chunks: t.List[t.Tuple[t.List[Token], ChunkType]] = [([], ChunkType.SQL)]
    total = len(tokens)

    pos = 0
    virtual = False
    while pos < total:
        token = tokens[pos]
        if _is_virtual_statement_end(tokens, pos):
            chunks[-1][0].append(token)
            virtual = False
            chunks.append(([], ChunkType.SQL))
            pos += 2
        elif _is_jinja_end(tokens, pos) or (
            chunks[-1][1] == ChunkType.SQL
            and token.token_type == TokenType.SEMICOLON
            and pos < total - 1
        ):
            if token.token_type == TokenType.SEMICOLON:
                pos += 1
            else:
                # Jinja end statement
                chunks[-1][0].append(token)
                pos += 2
            chunks.append(
                (
                    [],
                    ChunkType.VIRTUAL_STATEMENT
                    if virtual and tokens[pos] != ON_VIRTUAL_UPDATE_END
                    else ChunkType.SQL,
                )
            )
        elif _is_jinja_query_begin(tokens, pos):
            chunks.append(([token], ChunkType.JINJA_QUERY))
            pos += 2
        elif _is_jinja_statement_begin(tokens, pos):
            chunks.append(([token], ChunkType.JINJA_STATEMENT))
            pos += 2
        elif _is_virtual_statement_begin(tokens, pos):
            chunks.append(([token], ChunkType.VIRTUAL_STATEMENT))
            pos += 2
            virtual = True
        else:
            chunks[-1][0].append(token)
            pos += 1

    parser = dialect.parser()
    expressions: t.List[exp.Expression] = []

    def parse_sql_chunk(chunk: t.List[Token], meta_sql: bool = True) -> t.List[exp.Expression]:
        parsed_expressions: t.List[t.Optional[exp.Expression]] = (
            parser.parse(chunk, sql) if into is None else parser.parse_into(into, chunk, sql)
        )
        expressions = []
        for expression in parsed_expressions:
            if expression:
                if meta_sql:
                    expression.meta["sql"] = parser._find_sql(chunk[0], chunk[-1])
                expressions.append(expression)
        return expressions

    def parse_jinja_chunk(chunk: t.List[Token], meta_sql: bool = True) -> exp.Expression:
        start, *_, end = chunk
        segment = sql[start.end + 2 : end.start - 1]
        factory = jinja_query if chunk_type == ChunkType.JINJA_QUERY else jinja_statement
        expression = factory(segment.strip())
        if meta_sql:
            expression.meta["sql"] = sql[start.start : end.end + 1]
        return expression

    def parse_virtual_statement(
        chunks: t.List[t.Tuple[t.List[Token], ChunkType]], pos: int
    ) -> t.Tuple[t.List[exp.Expression], int]:
        # For virtual statements we need to handle both SQL and Jinja nested blocks within the chunk
        virtual_update_statements = []
        start = chunks[pos][0][0].start

        while (
            chunks[pos - 1][0] == [] or chunks[pos - 1][0][-1].text.upper() != ON_VIRTUAL_UPDATE_END
        ):
            chunk, chunk_type = chunks[pos]
            if chunk_type == ChunkType.JINJA_STATEMENT:
                virtual_update_statements.append(parse_jinja_chunk(chunk, False))
            else:
                virtual_update_statements.extend(
                    parse_sql_chunk(
                        chunk[int(chunk[0].text.upper() == ON_VIRTUAL_UPDATE_BEGIN) : -1], False
                    ),
                )
            pos += 1

        if virtual_update_statements:
            statements = virtual_statement(virtual_update_statements)
            end = chunk[-1].end + 1
            statements.meta["sql"] = sql[start:end]
            return [statements], pos

        return [], pos

    pos = 0
    total_chunks = len(chunks)
    while pos < total_chunks:
        chunk, chunk_type = chunks[pos]
        if chunk_type == ChunkType.VIRTUAL_STATEMENT:
            virtual_expression, pos = parse_virtual_statement(chunks, pos)
            expressions.extend(virtual_expression)
        elif chunk_type == ChunkType.SQL:
            expressions.extend(parse_sql_chunk(chunk))
        else:
            expressions.append(parse_jinja_chunk(chunk))
        pos += 1

    return expressions


def extend_sqlglot() -> None:
    """Extend SQLGlot with SQLMesh's custom macro aware dialect."""
    tokenizers = {Tokenizer}
    parsers = {Parser}
    generators = {Generator}

    for dialect in Dialect.classes.values():
        # Athena picks a different Tokenizer / Parser / Generator depending on the query
        # so this ensures that the extra ones it defines are also extended
        if dialect == athena.Athena:
            tokenizers.add(athena._TrinoTokenizer)
            parsers.add(athena._TrinoParser)
            generators.add(athena._TrinoGenerator)
            generators.add(athena._HiveGenerator)

        if hasattr(dialect, "Tokenizer"):
            tokenizers.add(dialect.Tokenizer)
        if hasattr(dialect, "Parser"):
            parsers.add(dialect.Parser)
        if hasattr(dialect, "Generator"):
            generators.add(dialect.Generator)

    for tokenizer in tokenizers:
        tokenizer.VAR_SINGLE_TOKENS.update(SQLMESH_MACRO_PREFIX)

    for parser in parsers:
        parser.FUNCTIONS.update({"JINJA": Jinja.from_arg_list, "METRIC": MetricAgg.from_arg_list})
        parser.PLACEHOLDER_PARSERS.update({TokenType.PARAMETER: _parse_macro})
        parser.QUERY_MODIFIER_PARSERS.update(
            {TokenType.PARAMETER: lambda self: _parse_body_macro(self)}
        )

    for generator in generators:
        if MacroFunc not in generator.TRANSFORMS:
            generator.TRANSFORMS.update(
                {
                    Audit: lambda self, e: _sqlmesh_ddl_sql(self, e, "AUDIT"),
                    DColonCast: lambda self, e: f"{self.sql(e, 'this')}::{self.sql(e, 'to')}",
                    Jinja: lambda self, e: e.name,
                    JinjaQuery: lambda self, e: f"{JINJA_QUERY_BEGIN};\n{e.name}\n{JINJA_END};",
                    JinjaStatement: lambda self,
                    e: f"{JINJA_STATEMENT_BEGIN};\n{e.name}\n{JINJA_END};",
                    VirtualUpdateStatement: lambda self, e: _on_virtual_update_sql(self, e),
                    MacroDef: lambda self, e: f"@DEF({self.sql(e.this)}, {self.sql(e.expression)})",
                    MacroFunc: _macro_func_sql,
                    MacroStrReplace: lambda self, e: f"@{self.sql(e.this)}",
                    MacroSQL: lambda self, e: f"@SQL({self.sql(e.this)})",
                    MacroVar: lambda self, e: f"@{e.name}",
                    Metric: lambda self, e: _sqlmesh_ddl_sql(self, e, "METRIC"),
                    Model: lambda self, e: _sqlmesh_ddl_sql(self, e, "MODEL"),
                    ModelKind: _model_kind_sql,
                    PythonCode: lambda self, e: self.expressions(e, sep="\n", indent=False),
                    StagedFilePath: lambda self, e: self.table_sql(e),
                    exp.Whens: _whens_sql,
                }
            )
        if MacroDef not in generator.WITH_SEPARATED_COMMENTS:
            generator.WITH_SEPARATED_COMMENTS = (
                *generator.WITH_SEPARATED_COMMENTS,
                Model,
                MacroDef,
            )

        generator.UNWRAPPED_INTERVAL_VALUES = (
            *generator.UNWRAPPED_INTERVAL_VALUES,
            MacroStrReplace,
            MacroVar,
        )

    _override(Parser, _parse_select)
    _override(Parser, _parse_statement)
    _override(Parser, _parse_join)
    _override(Parser, _parse_order)
    _override(Parser, _parse_where)
    _override(Parser, _parse_group)
    _override(Parser, _parse_with)
    _override(Parser, _parse_having)
    _override(Parser, _parse_limit)
    _override(Parser, _parse_value)
    _override(Parser, _parse_lambda)
    _override(Parser, _parse_types)
    _override(Parser, _parse_if)
    _override(Parser, _parse_id_var)
    _override(Parser, _warn_unsupported)
    _override(Snowflake.Parser, _parse_table_parts)

    # DuckDB's prefix absolute power operator `@` clashes with the macro syntax
    DuckDB.Parser.NO_PAREN_FUNCTION_PARSERS.pop("@", None)


def select_from_values(
    values: t.List[t.Tuple[t.Any, ...]],
    columns_to_types: t.Dict[str, exp.DataType],
    batch_size: int = 0,
    alias: str = "t",
) -> t.Iterator[exp.Select]:
    """Generate a VALUES expression that has a select wrapped around it to cast the values to their correct types.

    Args:
        values: List of values to use for the VALUES expression.
        columns_to_types: Mapping of column names to types to assign to the values.
        batch_size: The maximum number of tuples per batches. Defaults to sys.maxsize if <= 0.
        alias: The alias to assign to the values expression. If not provided then will default to "t"

    Returns:
        This method operates as a generator and yields a VALUES expression.
    """
    if batch_size <= 0:
        batch_size = sys.maxsize
    num_rows = len(values)
    for i in range(0, num_rows, batch_size):
        yield select_from_values_for_batch_range(
            values=values,
            target_columns_to_types=columns_to_types,
            batch_start=i,
            batch_end=min(i + batch_size, num_rows),
            alias=alias,
        )


def select_from_values_for_batch_range(
    values: t.List[t.Tuple[t.Any, ...]],
    target_columns_to_types: t.Dict[str, exp.DataType],
    batch_start: int,
    batch_end: int,
    alias: str = "t",
    source_columns: t.Optional[t.List[str]] = None,
) -> exp.Select:
    source_columns = source_columns or list(target_columns_to_types)
    source_columns_to_types = get_source_columns_to_types(target_columns_to_types, source_columns)

    if not values:
        # Ensures we don't generate an empty VALUES clause & forces a zero-row output
        where = exp.false()
        expressions = [
            tuple(exp.cast(exp.null(), to=kind) for kind in source_columns_to_types.values())
        ]
    else:
        where = None
        expressions = [
            tuple(transform_values(v, source_columns_to_types))
            for v in values[batch_start:batch_end]
        ]

    values_exp = exp.values(expressions, alias=alias, columns=source_columns_to_types)
    if values:
        # BigQuery crashes on `SELECT CAST(x AS TIMESTAMP) FROM UNNEST([NULL]) AS x`, but not
        # on `SELECT CAST(x AS TIMESTAMP) FROM UNNEST([CAST(NULL AS TIMESTAMP)]) AS x`. This
        # ensures nulls under the `Values` expression are cast to avoid similar issues.
        for value, kind in zip(
            values_exp.expressions[0].expressions, source_columns_to_types.values()
        ):
            if isinstance(value, exp.Null):
                value.replace(exp.cast(value, to=kind))

    casted_columns = [
        exp.alias_(
            exp.cast(
                exp.column(column) if column in source_columns_to_types else exp.Null(), to=kind
            ),
            column,
            copy=False,
        )
        for column, kind in target_columns_to_types.items()
    ]
    return exp.select(*casted_columns).from_(values_exp, copy=False).where(where, copy=False)


def pandas_to_sql(
    df: pd.DataFrame,
    columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    batch_size: int = 0,
    alias: str = "t",
) -> t.Iterator[exp.Select]:
    """Convert a pandas dataframe into a VALUES sql statement.

    Args:
        df: A pandas dataframe to convert.
        columns_to_types: Mapping of column names to types to assign to the values.
        batch_size: The maximum number of tuples per batches. Defaults to sys.maxsize if <= 0.
        alias: The alias to assign to the values expression. If not provided then will default to "t"

    Returns:
        This method operates as a generator and yields a VALUES expression.
    """
    yield from select_from_values(
        values=list(df.itertuples(index=False, name=None)),
        columns_to_types=columns_to_types or columns_to_types_from_df(df),
        batch_size=batch_size,
        alias=alias,
    )


def set_default_catalog(
    table: str | exp.Table,
    default_catalog: t.Optional[str],
) -> exp.Table:
    table = exp.to_table(table)

    if default_catalog and not table.catalog and table.db:
        table.set("catalog", exp.parse_identifier(default_catalog))

    return table


@lru_cache(maxsize=16384)
def normalize_model_name(
    table: str | exp.Table | exp.Column,
    default_catalog: t.Optional[str],
    dialect: DialectType = None,
) -> str:
    if isinstance(table, exp.Column):
        table = exp.table_(table.this, db=table.args.get("table"), catalog=table.args.get("db"))
    else:
        # We are relying on sqlglot's flexible parsing here to accept quotes from other dialects.
        # Ex: I have a a normalized name of '"my_table"' but the dialect is spark and therefore we should
        # expect spark quotes to be backticks ('`') instead of double quotes ('"'). sqlglot today is flexible
        # and will still parse this correctly and we rely on that.
        table = exp.to_table(table, dialect=dialect)

    table = set_default_catalog(table, default_catalog)
    # An alternative way to do this is the following: exp.table_name(table, dialect=dialect, identify=True)
    # This though would result in the names being normalized to the target dialect AND the quotes while the below
    # approach just normalizes the names.
    # By just normalizing names and using sqlglot dialect for quotes this makes it easier for dialects that have
    # compatible normalization strategies but incompatible quoting to still work together without user hassle
    return exp.table_name(normalize_identifiers(table, dialect=dialect), identify=True)


def find_tables(
    expression: exp.Expression, default_catalog: t.Optional[str], dialect: DialectType = None
) -> t.Set[str]:
    """Find all tables referenced in a query.

    Caches the result in the meta field 'tables'.

    Args:
        expressions: The query to find the tables in.
        dialect: The dialect to use for normalization of table names.

    Returns:
        A Set of all the table names.
    """
    if TABLES_META not in expression.meta:
        expression.meta[TABLES_META] = {
            normalize_model_name(table, default_catalog=default_catalog, dialect=dialect)
            for scope in traverse_scope(expression)
            for table in scope.tables
            if table.name and table.name not in scope.cte_sources
        }
    return expression.meta[TABLES_META]


def add_table(node: exp.Expression, table: str) -> exp.Expression:
    """Add a table to all columns in an expression."""

    def _transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table:
            return exp.column(node.this, table=table)
        if isinstance(node, exp.Identifier):
            return exp.column(node, table=table)
        return node

    return node.transform(_transform)


def transform_values(
    values: t.Tuple[t.Any, ...], columns_to_types: t.Dict[str, exp.DataType]
) -> t.Iterator[t.Any]:
    """Perform transformations on values given columns_to_types."""

    def _transform_value(value: t.Any, dtype: exp.DataType) -> t.Any:
        if (
            isinstance(value, list)
            and dtype.is_type(*exp.DataType.ARRAY_TYPES)
            and len(dtype.expressions) == 1
        ):
            element_type = dtype.expressions[0]
            return exp.convert([_transform_value(v, element_type) for v in value])

        if (
            isinstance(value, dict)
            and dtype.is_type(*exp.DataType.STRUCT_TYPES)
            and len(value) == len(dtype.expressions)
        ):
            expressions = []
            for (field_name, field_value), field_type in zip(value.items(), dtype.expressions):
                if isinstance(field_type, exp.ColumnDef):
                    field_type = field_type.kind
                else:
                    field_type = exp.DataType.build(exp.DataType.Type.UNKNOWN)

                expressions.append(
                    exp.PropertyEQ(
                        this=exp.to_identifier(field_name),
                        expression=_transform_value(field_value, field_type),
                    )
                )

            return exp.Struct(expressions=expressions)

        if dtype.is_type(exp.DataType.Type.JSON):
            return exp.func("PARSE_JSON", f"'{value}'")

        return exp.convert(value)

    for col_value, col_type in zip(values, columns_to_types.values()):
        yield _transform_value(col_value, col_type)


def to_schema(sql_path: str | exp.Table, dialect: DialectType = None) -> exp.Table:
    if isinstance(sql_path, exp.Table) and sql_path.this is None:
        return sql_path
    table = exp.to_table(
        sql_path.copy() if isinstance(sql_path, exp.Table) else sql_path, dialect=dialect
    )
    table.set("catalog", table.args.get("db"))
    table.set("db", table.args.get("this"))
    table.set("this", None)
    return table


def schema_(
    db: exp.Identifier | str,
    catalog: t.Optional[exp.Identifier | str] = None,
    quoted: t.Optional[bool] = None,
) -> exp.Table:
    """Build a Schema.

    Args:
        db: Database name.
        catalog: Catalog name.
        quoted: Whether to force quotes on the schema's identifiers.

    Returns:
        The new Schema instance.
    """
    return exp.Table(
        this=None,
        db=exp.to_identifier(db, quoted=quoted) if db else None,
        catalog=exp.to_identifier(catalog, quoted=quoted) if catalog else None,
    )


def normalize_mapping_schema(schema: t.Dict, dialect: DialectType) -> MappingSchema:
    return MappingSchema(_unquote_schema(schema), dialect=dialect, normalize=False)


def _unquote_schema(schema: t.Dict) -> t.Dict:
    """SQLGlot schema expects unquoted normalized keys."""
    return {
        k.strip('"'): _unquote_schema(v) if isinstance(v, dict) else v for k, v in schema.items()
    }


@contextmanager
def normalize_and_quote(
    query: E, dialect: DialectType, default_catalog: t.Optional[str], quote: bool = True
) -> t.Iterator[E]:
    qualify_tables(query, catalog=default_catalog, dialect=dialect)
    normalize_identifiers(query, dialect=dialect)
    yield query
    if quote:
        quote_identifiers(query, dialect=dialect)


def interpret_expression(e: exp.Expression) -> exp.Expression | str | int | float | bool:
    if e.is_int:
        return int(e.this)
    if e.is_number:
        return float(e.this)
    if isinstance(e, (exp.Literal, exp.Boolean)):
        return e.this
    return e


def interpret_key_value_pairs(
    e: exp.Tuple,
) -> t.Dict[str, exp.Expression | str | int | float | bool]:
    return {i.this.name: interpret_expression(i.expression) for i in e.expressions}


def extract_func_call(
    v: exp.Expression, allow_tuples: bool = False
) -> t.Tuple[str, t.Dict[str, exp.Expression]]:
    kwargs = {}

    if isinstance(v, exp.Anonymous):
        func = v.name
        args = v.expressions
    elif isinstance(v, exp.Func):
        func = v.sql_name()
        args = list(v.args.values())
    elif isinstance(v, exp.Paren):
        func = ""
        args = [v.this]
    elif isinstance(v, exp.Tuple):  # airflow only
        if not allow_tuples:
            raise ConfigError("Audit name is missing (eg. MY_AUDIT())")

        func = ""
        args = v.expressions
    else:
        return v.name.lower(), {}

    for arg in args:
        if not isinstance(arg, (exp.PropertyEQ, exp.EQ)):
            raise ConfigError(
                f"Function '{func}' must be called with key-value arguments like {func}(arg := value)."
            )
        kwargs[arg.left.name.lower()] = arg.right
    return func.lower(), kwargs


def extract_function_calls(func_calls: t.Any, allow_tuples: bool = False) -> t.Any:
    """Used for extracting function calls for signals or audits."""

    if isinstance(func_calls, (exp.Tuple, exp.Array)):
        return [extract_func_call(i, allow_tuples=allow_tuples) for i in func_calls.expressions]
    if isinstance(func_calls, exp.Paren):
        return [extract_func_call(func_calls.this, allow_tuples=allow_tuples)]
    if isinstance(func_calls, exp.Expression):
        return [extract_func_call(func_calls, allow_tuples=allow_tuples)]
    if isinstance(func_calls, list):
        function_calls = []
        for entry in func_calls:
            if isinstance(entry, dict):
                args = entry
                name = "" if allow_tuples else entry.pop("name")
            elif isinstance(entry, (tuple, list)):
                name, args = entry
            else:
                raise ConfigError(f"Audit must be a dictionary or named tuple. Got {entry}.")

            function_calls.append(
                (
                    name.lower(),
                    {
                        key: parse_one(value) if isinstance(value, str) else value
                        for key, value in args.items()
                    },
                )
            )

        return function_calls

    return func_calls or []


def is_meta_expression(v: t.Any) -> bool:
    return isinstance(v, (Audit, Metric, Model))


def replace_merge_table_aliases(
    expression: exp.Expression, dialect: t.Optional[str] = None
) -> exp.Expression:
    """
    Resolves references from the "source" and "target" tables (or their DBT equivalents)
    with the corresponding SQLMesh merge aliases (MERGE_SOURCE_ALIAS and MERGE_TARGET_ALIAS)
    """
    from sqlmesh.core.engine_adapter.base import MERGE_SOURCE_ALIAS, MERGE_TARGET_ALIAS

    if isinstance(expression, exp.Column) and (first_part := expression.parts[0]):
        if first_part.this.lower() in ("target", "dbt_internal_dest", "__merge_target__"):
            first_part.replace(exp.to_identifier(MERGE_TARGET_ALIAS, quoted=True))
        elif first_part.this.lower() in ("source", "dbt_internal_source", "__merge_source__"):
            first_part.replace(exp.to_identifier(MERGE_SOURCE_ALIAS, quoted=True))

    return expression
