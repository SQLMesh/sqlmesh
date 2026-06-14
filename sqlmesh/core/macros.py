from __future__ import annotations

import inspect
import sys
import types
import typing as t
from enum import Enum
from functools import lru_cache, reduce
from itertools import chain
from pathlib import Path
from string import Template
from datetime import datetime, date

import sqlglot
from sqlglot import Generator, exp, parse_one
from sqlglot.executor.env import ENV
from sqlglot.executor.python import Python
from sqlglot.generators.python import PythonGenerator
from sqlglot.helper import csv, ensure_collection
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.schema import MappingSchema

from sqlmesh.core import constants as c
from sqlmesh.core.dialect import (
    SQLMESH_MACRO_PREFIX,
    Dialect,
    MacroDef,
    MacroFunc,
    MacroSQL,
    MacroStrReplace,
    MacroVar,
    StagedFilePath,
    normalize_model_name,
)
from sqlmesh.utils import (
    DECORATOR_RETURN_TYPE,
    UniqueKeyDict,
    columns_to_types_all_known,
    registry_decorator,
)
from sqlmesh.utils.date import DatetimeRanges, to_datetime, to_date
from sqlmesh.utils.errors import MacroEvalError, SQLMeshError
from sqlmesh.utils.metaprogramming import (
    Executable,
    SqlValue,
    format_evaluated_code_exception,
    prepare_env,
)

if t.TYPE_CHECKING:
    from sqlglot.dialects.dialect import DialectType
    from sqlmesh.core._typing import TableName
    from sqlmesh.core.engine_adapter import EngineAdapter
    from sqlmesh.core.snapshot import Snapshot
    from sqlmesh.core.environment import EnvironmentNamingInfo


if sys.version_info >= (3, 10):
    UNION_TYPES = (t.Union, types.UnionType)
else:
    UNION_TYPES = (t.Union,)


class RuntimeStage(Enum):
    LOADING = "loading"
    CREATING = "creating"
    EVALUATING = "evaluating"
    PROMOTING = "promoting"
    DEMOTING = "demoting"
    AUDITING = "auditing"
    TESTING = "testing"
    BEFORE_ALL = "before_all"
    AFTER_ALL = "after_all"


class MacroStrTemplate(Template):
    delimiter = SQLMESH_MACRO_PREFIX


EXPRESSIONS_NAME_MAP = {}
SQL = t.NewType("SQL", str)


@lru_cache()
def get_supported_types() -> t.Dict[str, t.Any]:
    from sqlmesh.core.context import ExecutionContext

    return {
        "t": t,
        "typing": t,
        "List": t.List,
        "Tuple": t.Tuple,
        "Union": t.Union,
        "DatetimeRanges": DatetimeRanges,
        "exp": exp,
        "SQL": SQL,
        "MacroEvaluator": MacroEvaluator,
        "ExecutionContext": ExecutionContext,
    }


for klass in sqlglot.Parser.EXPRESSION_PARSERS:
    name = klass if isinstance(klass, str) else klass.__name__  # type: ignore
    EXPRESSIONS_NAME_MAP[name.lower()] = name


def _macro_sql(sql: str, into: t.Optional[str] = None) -> str:
    args = [_macro_str_replace(sql)]
    if into in EXPRESSIONS_NAME_MAP:
        args.append(f"into=exp.{EXPRESSIONS_NAME_MAP[into]}")
    return f"self.parse_one({', '.join(args)})"


def _macro_func_sql(self: Generator, e: exp.Expr) -> str:
    func = e.this

    if isinstance(func, exp.Anonymous):
        return f"""self.send({csv("'" + func.name + "'", self.expressions(func))})"""
    return self.sql(func)


def _macro_str_replace(text: str) -> str:
    """Stringifies python code for variable replacement
    Args:
        text: text string
    Returns:
        Stringified python code to execute variable replacement
    """
    return f"self.template({text}, locals())"


class CaseInsensitiveMapping(t.Dict[str, t.Any]):
    def __init__(self, data: t.Dict[str, t.Any]) -> None:
        super().__init__(data)

    def __getitem__(self, key: str) -> t.Any:
        return super().__getitem__(key.lower())

    def get(self, key: str, default: t.Any = None, /) -> t.Any:
        return super().get(key.lower(), default)


class MacroDialect(Python):
    class Generator(PythonGenerator):
        TRANSFORMS = {
            **PythonGenerator.TRANSFORMS,
            exp.Column: lambda self, e: f"exp.to_column('{self.sql(e, 'this')}')",
            exp.Lambda: lambda self, e: f"lambda {self.expressions(e)}: {self.sql(e, 'this')}",
            MacroFunc: _macro_func_sql,
            MacroSQL: lambda self, e: _macro_sql(self.sql(e, "this"), e.args.get("into")),
            MacroStrReplace: lambda self, e: _macro_str_replace(self.sql(e, "this")),
        }


class MacroEvaluator:
    """The class responsible for evaluating SQLMesh Macros/SQL.

    SQLMesh supports special preprocessed SQL prefixed with `@`. Although it provides similar power to
    traditional methods like string templating, there is semantic understanding of SQL which prevents
    common errors like leading/trailing commas, syntax errors, etc.

    SQLMesh SQL allows for macro variables and macro functions. Macro variables take the form of @variable. These are used for variable substitution.

    SELECT * FROM foo WHERE ds BETWEEN @start_date AND @end_date

    Macro variables can be defined with a special macro function.

    @DEF(start_date, '2021-01-01')

    Args:
        dialect: Dialect of the SQL to evaluate.
        python_env: Serialized Python environment.
    """

    def __init__(
        self,
        dialect: DialectType = "",
        python_env: t.Optional[t.Dict[str, Executable]] = None,
        schema: t.Optional[MappingSchema] = None,
        runtime_stage: RuntimeStage = RuntimeStage.LOADING,
        resolve_table: t.Optional[t.Callable[[str | exp.Table], str]] = None,
        resolve_tables: t.Optional[t.Callable[[exp.Expr], exp.Expr]] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        default_catalog: t.Optional[str] = None,
        path: t.Optional[Path] = None,
        environment_naming_info: t.Optional[EnvironmentNamingInfo] = None,
        model_fqn: t.Optional[str] = None,
    ):
        self.dialect = dialect
        self.generator = MacroDialect().generator()
        self.locals: t.Dict[str, t.Any] = {
            "runtime_stage": runtime_stage.value,
            "default_catalog": default_catalog,
        }
        self.env = {
            **ENV,
            "self": self,
            "SQL": SQL,
            "MacroEvaluator": MacroEvaluator,
        }
        self.python_env = python_env or {}
        self.macros = {normalize_macro_name(k): v.func for k, v in macro.get_registry().items()}
        self.columns_to_types_called = False
        self.default_catalog = default_catalog

        self._schema = schema
        self._resolve_table = resolve_table
        self._resolve_tables = resolve_tables
        self._snapshots = snapshots if snapshots is not None else {}
        self._path = path
        self._environment_naming_info = environment_naming_info
        self._model_fqn = model_fqn

        prepare_env(self.python_env, self.env)
        for k, v in self.python_env.items():
            if v.is_definition:
                self.macros[normalize_macro_name(k)] = self.env[v.name or k]
            elif v.is_import and getattr(self.env.get(k), c.SQLMESH_MACRO, None):
                self.macros[normalize_macro_name(k)] = self.env[k]
            elif v.is_value:
                value = self.env[k]
                if k in (
                    c.SQLMESH_VARS,
                    c.SQLMESH_VARS_METADATA,
                    c.SQLMESH_BLUEPRINT_VARS,
                    c.SQLMESH_BLUEPRINT_VARS_METADATA,
                ):
                    value = {
                        var_name: (
                            self.parse_one(var_value.sql)
                            if isinstance(var_value, SqlValue)
                            else var_value
                        )
                        for var_name, var_value in value.items()
                    }

                self.locals[k] = value

    def send(
        self, name: str, *args: t.Any, **kwargs: t.Any
    ) -> t.Union[None, exp.Expr, t.List[exp.Expr]]:
        func = self.macros.get(normalize_macro_name(name))

        if not callable(func):
            raise MacroEvalError(f"Macro '{name}' does not exist.")

        try:
            return call_macro(
                func, self.dialect, self._path, provided_args=(self, *args), provided_kwargs=kwargs
            )  # type: ignore
        except Exception as e:
            raise MacroEvalError(
                f"An error occurred during evaluation of '{name}'\n\n"
                + format_evaluated_code_exception(e, self.python_env)
            )

    def transform(self, expression: exp.Expr) -> exp.Expr | t.List[exp.Expr] | None:
        changed = False

        def evaluate_macros(
            node: exp.Expr,
        ) -> exp.Expr | t.List[exp.Expr] | None:
            nonlocal changed

            if isinstance(node, MacroVar):
                changed = True
                variables = self.variables

                # This makes all variables case-insensitive, e.g. @X is the same as @x. We do this
                # for consistency, since `variables` and `blueprint_variables` are normalized.
                var_name = node.name.lower()

                if var_name not in self.locals and var_name not in variables:
                    if not isinstance(node.parent, StagedFilePath):
                        raise SQLMeshError(f"Macro variable '{node.name}' is undefined.")

                    return node

                # Precedence order is locals (e.g. @DEF) > blueprint variables > config variables
                value = self.locals.get(var_name, variables.get(var_name))
                if isinstance(value, list):
                    return exp.convert(
                        tuple(self.transform(v) if isinstance(v, exp.Expr) else v for v in value)
                    )

                return exp.convert(self.transform(value) if isinstance(value, exp.Expr) else value)
            if isinstance(node, exp.Identifier) and "@" in node.this:
                text = self.template(node.this, {})
                if node.this != text:
                    changed = True
                    return exp.to_identifier(text, quoted=node.quoted or None)
            if isinstance(node, MacroFunc):
                changed = True
                return self.evaluate(node)
            return node

        transformed = exp.replace_tree(
            expression.copy(),
            evaluate_macros,  # type: ignore[arg-type]
            prune=lambda n: isinstance(n, exp.Lambda),
        )

        if changed:
            # the transformations could have corrupted the ast, turning this into sql and reparsing ensures
            # that the ast is correct
            if isinstance(transformed, list):
                return [
                    self.parse_one(node.sql(dialect=self.dialect, copy=False))
                    for node in transformed
                ]
            if isinstance(transformed, exp.Expr):
                return self.parse_one(transformed.sql(dialect=self.dialect, copy=False))

        return transformed

    def template(self, text: t.Any, local_variables: t.Dict[str, t.Any]) -> str:
        """Substitute @vars with locals.

        Args:
            text: The string to do substitition on.
            local_variables: Local variables in the context so that lambdas can be used.

        Returns:
           The rendered string.
        """
        # We try to convert all variables into sqlglot expressions because they're going to be converted
        # into strings; in sql we don't convert strings because that would result in adding quotes
        base_mapping = {
            k.lower(): convert_sql(v, self.dialect)
            for k, v in chain(self.variables.items(), self.locals.items(), local_variables.items())
            if k.lower()
            not in (
                "engine_adapter",
                "snapshot",
            )
        }
        return MacroStrTemplate(str(text)).safe_substitute(CaseInsensitiveMapping(base_mapping))

    def evaluate(self, node: MacroFunc) -> exp.Expr | t.List[exp.Expr] | None:
        if isinstance(node, MacroDef):
            if isinstance(node.expression, exp.Lambda):
                _, fn = _norm_var_arg_lambda(self, node.expression)
                self.macros[normalize_macro_name(node.name)] = lambda _, *args: fn(
                    args[0] if len(args) == 1 else exp.Tuple(expressions=list(args))
                )
            else:
                # Make variables defined through `@DEF` case-insensitive
                self.locals[node.name.lower()] = self.transform(node.expression)

            return node

        if isinstance(node, (MacroSQL, MacroStrReplace)):
            result: t.Optional[exp.Expr | t.List[exp.Expr]] = exp.convert(
                self.eval_expression(node)
            )
        else:
            func = t.cast(exp.Anonymous, node.this)

            args = []
            kwargs = {}
            for e in func.expressions:
                if isinstance(e, exp.PropertyEQ):
                    kwargs[e.this.name] = e.expression
                else:
                    if kwargs:
                        raise MacroEvalError(
                            "Positional argument cannot follow keyword argument.\n  "
                            f"{func.sql(dialect=self.dialect)} at '{self._path}'"
                        )

                    args.append(e)

            result = self.send(func.name, *args, **kwargs)

        if result is None:
            return None

        if isinstance(result, (tuple, list)):
            result = [self.parse_one(item) for item in result if item is not None]

            if (
                len(result) == 1
                and isinstance(result[0], (exp.Array, exp.Tuple))
                and node.find_ancestor(MacroFunc)
            ):
                """
                if:
                 - the output of evaluating this node is being passed as an argument to another macro function
                 - and that output is something that _norm_var_arg_lambda() will unpack into varargs
                   > (a list containing a single item of type exp.Tuple/exp.Array)
                then we will get inconsistent behaviour depending on if this node emits a list with a single item vs multiple items.
                
                In the first case, emitting a list containing a single array item will cause that array to get unpacked and its *members* passed to the calling macro
                In the second case, emitting a list containing multiple array items will cause each item to get passed as-is to the calling macro
                
                To prevent this inconsistency, we wrap this node output in an exp.Array so that _norm_var_arg_lambda() can "unpack" that into the
                actual argument we want to pass to the parent macro function
                
                Note we only do this for evaluation results that get passed as an argument to another macro, because when the final
                result is given to something like SELECT, we still want that to be unpacked into a list of items like:
                 - SELECT ARRAY(1), ARRAY(2)
                rather than a single item like:
                 - SELECT ARRAY(ARRAY(1), ARRAY(2))                
                """
                result = [exp.Array(expressions=result)]
        else:
            result = self.parse_one(result)

        return result

    def eval_expression(self, node: t.Any) -> t.Any:
        """Converts a SQLGlot expression into executable Python code and evals it.

        If the node is not an expression, it will simply be returned.

        Args:
            node: expression
        Returns:
            The return value of the evaled Python Code.
        """
        if not isinstance(node, exp.Expr):
            return node
        code = node.sql()
        try:
            code = self.generator.generate(node)
            return eval(code, self.env, self.locals)
        except Exception as e:
            raise MacroEvalError(
                f"Error trying to eval macro.\n\nGenerated code: {code}\n\nOriginal sql: {node}\n\n"
                + format_evaluated_code_exception(e, self.python_env)
            )

    def parse_one(
        self, sql: str | exp.Expr, into: t.Optional[exp.IntoType] = None, **opts: t.Any
    ) -> exp.Expr:
        """Parses the given SQL string and returns a syntax tree for the first
        parsed SQL statement.

        Args:
            sql: the SQL code or expression to parse.
            into: the Expression to parse into
            **opts: other options

        Returns:
            Expression: the syntax tree for the first parsed statement
        """
        return sqlglot.maybe_parse(sql, dialect=self.dialect, into=into, **opts)

    def columns_to_types(self, model_name: TableName | exp.Column) -> t.Dict[str, exp.DataType]:
        """Returns the columns-to-types mapping corresponding to the specified model."""

        # We only return this dummy schema at load time, because if we don't actually know the
        # target model's schema at creation/evaluation time, returning a dummy schema could lead
        # to unintelligible errors when the query is executed
        if (self._schema is None or self._schema.empty) and self.runtime_stage == "loading":
            self.columns_to_types_called = True
            return {"__schema_unavailable_at_load__": exp.DataType.build("unknown")}

        normalized_model_name = normalize_model_name(
            model_name,
            default_catalog=self.default_catalog,
            dialect=self.dialect,
        )
        model_name = exp.to_table(normalized_model_name)

        columns_to_types = (
            self._schema.find(model_name, ensure_data_types=True) if self._schema else None
        )
        if columns_to_types is None:
            snapshot = self.get_snapshot(model_name)
            if snapshot and snapshot.node.is_model:
                columns_to_types = snapshot.node.columns_to_types  # type: ignore

        if columns_to_types is None:
            raise SQLMeshError(f"Schema for model '{model_name}' can't be statically determined.")

        return columns_to_types

    def get_snapshot(self, model_name: TableName | exp.Column) -> t.Optional[Snapshot]:
        """Returns the snapshot that corresponds to the given model name."""
        return self._snapshots.get(
            normalize_model_name(
                model_name,
                default_catalog=self.default_catalog,
                dialect=self.dialect,
            )
        )

    def resolve_table(self, table: str | exp.Table) -> str:
        """Gets the physical table name for a given model."""
        if not self._resolve_table:
            raise SQLMeshError(
                "Macro evaluator not properly initialized with resolve_table lambda."
            )
        return self._resolve_table(table)

    def resolve_tables(self, query: exp.Expr) -> exp.Expr:
        """Resolves queries with references to SQLMesh model names to their physical tables."""
        if not self._resolve_tables:
            raise SQLMeshError(
                "Macro evaluator not properly initialized with resolve_tables lambda."
            )
        return self._resolve_tables(query)

    @property
    def runtime_stage(self) -> RuntimeStage:
        """Returns the current runtime stage of the macro evaluation."""
        return self.locals["runtime_stage"]

    @property
    def this_model(self) -> str:
        """Returns the resolved name of the surrounding model."""
        this_model = self.locals.get("this_model")
        if not this_model:
            raise SQLMeshError("Model name is not available in the macro evaluator.")
        return this_model.sql(dialect=self.dialect, identify=True, comments=False)

    @property
    def this_model_fqn(self) -> str:
        if self._model_fqn is None:
            raise SQLMeshError("Model name is not available in the macro evaluator.")
        return self._model_fqn

    @property
    def engine_adapter(self) -> EngineAdapter:
        engine_adapter = self.locals.get("engine_adapter")
        if not engine_adapter:
            raise SQLMeshError(
                "The engine adapter is not available while models are loading."
                " You can gate these calls by checking in Python: evaluator.runtime_stage != 'loading' or SQL: @runtime_stage <> 'loading'."
            )
        return self.locals["engine_adapter"]

    @property
    def gateway(self) -> t.Optional[str]:
        """Returns the gateway name."""
        return self.var(c.GATEWAY)

    @property
    def snapshots(self) -> t.Dict[str, Snapshot]:
        """Returns the snapshots if available."""
        return self._snapshots

    @property
    def this_env(self) -> str:
        """Returns the name of the current environment in before after all."""
        if "this_env" not in self.locals:
            raise SQLMeshError("Environment name is only available in before_all and after_all")
        return self.locals["this_env"]

    @property
    def schemas(self) -> t.List[str]:
        """Returns the schemas of the current environment in before after all macros."""
        if "schemas" not in self.locals:
            raise SQLMeshError("Schemas are only available in before_all and after_all")
        return self.locals["schemas"]

    @property
    def views(self) -> t.List[str]:
        """Returns the views of the current environment in before after all macros."""
        if "views" not in self.locals:
            raise SQLMeshError("Views are only available in before_all and after_all")
        return self.locals["views"]

    def var(self, var_name: str, default: t.Optional[t.Any] = None) -> t.Optional[t.Any]:
        """Returns the value of the specified variable, or the default value if it doesn't exist."""
        return {
            **(self.locals.get(c.SQLMESH_VARS) or {}),
            **(self.locals.get(c.SQLMESH_VARS_METADATA) or {}),
        }.get(var_name.lower(), default)

    def blueprint_var(self, var_name: str, default: t.Optional[t.Any] = None) -> t.Optional[t.Any]:
        """Returns the value of the specified blueprint variable, or the default value if it doesn't exist."""
        return {
            **(self.locals.get(c.SQLMESH_BLUEPRINT_VARS) or {}),
            **(self.locals.get(c.SQLMESH_BLUEPRINT_VARS_METADATA) or {}),
        }.get(var_name.lower(), default)

    @property
    def variables(self) -> t.Dict[str, t.Any]:
        return {
            **self.locals.get(c.SQLMESH_VARS, {}),
            **self.locals.get(c.SQLMESH_VARS_METADATA, {}),
            **self.locals.get(c.SQLMESH_BLUEPRINT_VARS, {}),
            **self.locals.get(c.SQLMESH_BLUEPRINT_VARS_METADATA, {}),
        }

    def _coerce(self, expr: exp.Expr, typ: t.Any, strict: bool = False) -> t.Any:
        """Coerces the given expression to the specified type on a best-effort basis."""
        return _coerce(expr, typ, self.dialect, self._path, strict)


class macro(registry_decorator):
    """Specifies a function is a macro and registers it the global MACROS registry.

    Registered macros can be referenced in SQL statements to make queries more dynamic/cleaner.

    Example:
        from sqlglot import exp
        from sqlmesh.core.macros import MacroEvaluator, macro

        @macro()
        def add_one(evaluator: MacroEvaluator, column: exp.Literal) -> exp.Add:
            return evaluator.parse_one(f"{column} + 1")

    Args:
        name: A custom name for the macro, the default is the name of the function.
    """

    registry_name = "macros"

    def __init__(self, *args: t.Any, metadata_only: bool = False, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self.metadata_only = metadata_only

    def __call__(
        self, func: t.Callable[..., DECORATOR_RETURN_TYPE]
    ) -> t.Callable[..., DECORATOR_RETURN_TYPE]:
        if self.metadata_only:
            setattr(func, c.SQLMESH_METADATA, self.metadata_only)
        wrapper = super().__call__(func)

        # This is used to identify macros at runtime to unwrap during serialization.
        setattr(wrapper, c.SQLMESH_MACRO, True)
        return wrapper


ExecutableOrMacro = t.Union[Executable, macro]
MacroRegistry = UniqueKeyDict[str, ExecutableOrMacro]


def _norm_var_arg_lambda(
    evaluator: MacroEvaluator, func: exp.Lambda, *items: t.Any
) -> t.Tuple[t.Iterable, t.Callable]:
    """
    Converts sql literal array and lambda into actual python iterable + callable.

    In order to support expressions like @EACH([a, b, c], x -> @SQL('@x')), the lambda var x
    needs be passed to the local state.

    Args:
        evaluator: MacroEvaluator that invoked the macro
        func: Lambda SQLGlot expression.
        items: Array or items of SQLGlot expressions.
    """

    def substitute(
        node: exp.Expr, args: t.Dict[str, exp.Expr]
    ) -> exp.Expr | t.List[exp.Expr] | None:
        if isinstance(node, (exp.Identifier, exp.Var)):
            if not isinstance(node.parent, exp.Column):
                name = node.name.lower()
                if name in args:
                    return args[name].copy()
                if name in evaluator.locals:
                    return exp.convert(evaluator.locals[name])
            if SQLMESH_MACRO_PREFIX in node.name:
                return node.__class__(
                    this=evaluator.template(node.name, {k: v.name for k, v in args.items()})
                )
        elif isinstance(node, MacroFunc):
            local_copy = evaluator.locals.copy()
            evaluator.locals.update(args)
            result = evaluator.transform(node)
            evaluator.locals = local_copy
            return result
        return node

    if len(items) == 1:
        item = items[0]
        expressions = (
            item.expressions
            if isinstance(item, (exp.Array, exp.Tuple))
            else [item.this]
            if isinstance(item, exp.Paren)
            else item
        )
    else:
        expressions = items

    if not callable(func):
        return expressions, lambda args: func.this.transform(
            substitute,
            {
                expression.name.lower(): arg
                for expression, arg in zip(
                    func.expressions, args.expressions if isinstance(args, exp.Tuple) else [args]
                )
            },
        )

    return expressions, func


@macro()
def each(
    evaluator: MacroEvaluator,
    *args: t.Any,
) -> t.List[t.Any]:
    """Iterates through items calling func on each.

    If a func call on item returns None, it will be excluded from the list.

    Args:
        evaluator: MacroEvaluator that invoked the macro
        args: The last argument should be a lambda of the form x -> x +1. The first argument can be
            an Array or var args can be used.

    Returns:
        A list of items that is the result of func
    """
    *items, func = args
    items, func = _norm_var_arg_lambda(evaluator, func, *items)  # type: ignore
    return [item for item in map(func, ensure_collection(items)) if item is not None]


@macro("IF")
def if_(
    evaluator: MacroEvaluator,
    condition: t.Any,
    true: t.Any,
    false: t.Any = None,
) -> t.Any:
    """Evaluates a given condition and returns the second argument if true or else the third argument.

    If false is not passed in, the default return value will be None.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> MacroEvaluator().transform(parse_one("@IF('a' = 1, a, b)")).sql()
        'b'

        >>> MacroEvaluator().transform(parse_one("@IF('a' = 1, a)"))
    """

    if evaluator.eval_expression(condition):
        return true
    return false


@macro("REDUCE")
def reduce_(evaluator: MacroEvaluator, *args: t.Any) -> t.Any:
    """Iterates through items applying provided function that takes two arguments
    cumulatively to the items of iterable items, from left to right, so as to reduce
    the iterable to a single item.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "@SQL(@REDUCE([100, 200, 300, 400], (x, y) -> x + y))"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        '1000'

    Args:
        evaluator: MacroEvaluator that invoked the macro
        args: The last argument should be a lambda of the form (x, y) -> x + y. The first argument can be
            an Array or var args can be used.
    Returns:
        A single item that is the result of applying func cumulatively to items
    """
    *items, func = args
    items, func = _norm_var_arg_lambda(evaluator, func, *items)  # type: ignore
    return reduce(lambda a, b: func(exp.Tuple(expressions=[a, b])), ensure_collection(items))


@macro("FILTER")
def filter_(evaluator: MacroEvaluator, *args: t.Any) -> t.List[t.Any]:
    """Iterates through items, applying provided function to each item and removing
    all items where the function returns False

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "@REDUCE(@FILTER([1, 2, 3], x -> x > 1), (x, y) -> x + y)"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        '2 + 3'

        >>> sql = "@EVAL(@REDUCE(@FILTER([1, 2, 3], x -> x > 1), (x, y) -> x + y))"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        '5'

    Args:
        evaluator: MacroEvaluator that invoked the macro
        args: The last argument should be a lambda of the form x -> x > 1. The first argument can be
            an Array or var args can be used.
    Returns:
        The items for which the func returned True
    """
    *items, func = args
    items, func = _norm_var_arg_lambda(evaluator, func, *items)  # type: ignore
    return list(filter(lambda arg: evaluator.eval_expression(func(arg)), items))


def _optional_expression(
    evaluator: MacroEvaluator,
    condition: exp.Condition,
    expression: exp.Expr,
) -> t.Optional[exp.Expr]:
    """Inserts expression when the condition is True

    The following examples express the usage of this function in the context of the macros which wrap it.

    Examples:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "@WITH(True) all_cities as (select * from city) select all_cities"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'WITH all_cities AS (SELECT * FROM city) SELECT all_cities'
        >>> sql = "select * from city left outer @JOIN(True) country on city.country = country.name"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'SELECT * FROM city LEFT OUTER JOIN country ON city.country = country.name'
        >>> sql = "select * from city @GROUP_BY(True) country, population"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'SELECT * FROM city GROUP BY country, population'
        >>> sql = "select * from city group by country @HAVING(True) population > 100 and country = 'Mexico'"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        "SELECT * FROM city GROUP BY country HAVING population > 100 AND country = 'Mexico'"

    Args:
        evaluator: MacroEvaluator that invoked the macro
        condition: Condition expression
        expression: SQL expression
    Returns:
        Expression if the conditional is True; otherwise None
    """
    return expression if evaluator.eval_expression(condition) else None


with_ = macro("WITH")(_optional_expression)
join = macro("JOIN")(_optional_expression)
where = macro("WHERE")(_optional_expression)
group_by = macro("GROUP_BY")(_optional_expression)
having = macro("HAVING")(_optional_expression)
order_by = macro("ORDER_BY")(_optional_expression)
limit = macro("LIMIT")(_optional_expression)


@macro("eval")
def eval_(evaluator: MacroEvaluator, condition: exp.Condition) -> t.Any:
    """Evaluate the given condition in a Python/SQL interpretor.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "@EVAL(1 + 1)"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        '2'
    """
    return evaluator.eval_expression(condition)


# macros with union types need to use t.Union since | isn't available until 3.9
@macro()
def star(
    evaluator: MacroEvaluator,
    relation: exp.Table,
    alias: exp.Column = t.cast(exp.Column, exp.column("")),
    exclude: t.Union[exp.Array, exp.Tuple] = exp.Tuple(expressions=[]),
    prefix: exp.Literal = exp.Literal.string(""),
    suffix: exp.Literal = exp.Literal.string(""),
    quote_identifiers: exp.Boolean = exp.true(),
    except_: t.Union[exp.Array, exp.Tuple] = exp.Tuple(expressions=[]),
) -> t.List[exp.Expr]:
    """Returns a list of projections for the given relation.

    Args:
        evaluator: MacroEvaluator that invoked the macro
        relation: The relation to select star from
        alias: The alias of the relation
        exclude: Columns to exclude
        prefix: A prefix to use for all selections
        suffix: A suffix to use for all selections
        quote_identifiers: Whether or not quote the resulting aliases, defaults to true
        except_: Alias for exclude (TODO: deprecate this, update docs)

    Returns:
        An array of columns.

    Example:
        >>> from sqlglot import parse_one, exp
        >>> from sqlglot.schema import MappingSchema
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "SELECT @STAR(foo, bar, exclude := [c], prefix := 'baz_') FROM foo AS bar"
        >>> MacroEvaluator(schema=MappingSchema({"foo": {"a": exp.DataType.build("string"), "b": exp.DataType.build("string"), "c": exp.DataType.build("string"), "d": exp.DataType.build("int")}})).transform(parse_one(sql)).sql()
        'SELECT CAST("bar"."a" AS TEXT) AS "baz_a", CAST("bar"."b" AS TEXT) AS "baz_b", CAST("bar"."d" AS INT) AS "baz_d" FROM foo AS bar'
    """
    if alias and not isinstance(alias, (exp.Identifier, exp.Column)):
        raise SQLMeshError(f"Invalid alias '{alias}'. Expected an identifier.")
    if exclude and not isinstance(exclude, (exp.Array, exp.Tuple)):
        raise SQLMeshError(f"Invalid exclude '{exclude}'. Expected an array.")
    if except_ != exp.tuple_():
        from sqlmesh.core.console import get_console

        get_console().log_warning(
            "The 'except_' argument in @STAR will soon be deprecated. Use 'exclude' instead."
        )
        if not isinstance(exclude, (exp.Array, exp.Tuple)):
            raise SQLMeshError(f"Invalid exclude_ '{exclude}'. Expected an array.")
    if prefix and not isinstance(prefix, exp.Literal):
        raise SQLMeshError(f"Invalid prefix '{prefix}'. Expected a literal.")
    if suffix and not isinstance(suffix, exp.Literal):
        raise SQLMeshError(f"Invalid suffix '{suffix}'. Expected a literal.")
    if not isinstance(quote_identifiers, exp.Boolean):
        raise SQLMeshError(f"Invalid quote_identifiers '{quote_identifiers}'. Expected a boolean.")

    excluded_names = {
        normalize_identifiers(excluded, dialect=evaluator.dialect).name
        for excluded in exclude.expressions or except_.expressions
    }
    quoted = quote_identifiers.this
    table_identifier = normalize_identifiers(
        alias if alias.name else relation, dialect=evaluator.dialect
    ).name

    columns_to_types = {
        k: v for k, v in evaluator.columns_to_types(relation).items() if k not in excluded_names
    }
    if columns_to_types_all_known(columns_to_types):
        return [
            exp.cast(
                exp.column(column, table=table_identifier, quoted=quoted),
                dtype,
                dialect=evaluator.dialect,
            ).as_(f"{prefix.this}{column}{suffix.this}", quoted=quoted)
            for column, dtype in columns_to_types.items()
        ]
    return [
        exp.column(column, table=table_identifier, quoted=quoted).as_(
            f"{prefix.this}{column}{suffix.this}", quoted=quoted
        )
        for column, type_ in columns_to_types.items()
    ]


@macro()
def generate_surrogate_key(
    evaluator: MacroEvaluator,
    *fields: exp.Expr,
    hash_function: exp.Literal = exp.Literal.string("MD5"),
) -> exp.Func:
    """Generates a surrogate key (string) for the given fields.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>>
        >>> sql = "SELECT @GENERATE_SURROGATE_KEY(a, b, c) FROM foo"
        >>> MacroEvaluator(dialect="bigquery").transform(parse_one(sql, dialect="bigquery")).sql("bigquery")
        "SELECT TO_HEX(MD5(CONCAT(COALESCE(CAST(a AS STRING), '_sqlmesh_surrogate_key_null_'), '|', COALESCE(CAST(b AS STRING), '_sqlmesh_surrogate_key_null_'), '|', COALESCE(CAST(c AS STRING), '_sqlmesh_surrogate_key_null_')))) FROM foo"
        >>>
        >>> sql = "SELECT @GENERATE_SURROGATE_KEY(a, b, c, hash_function := 'SHA256') FROM foo"
        >>> MacroEvaluator(dialect="bigquery").transform(parse_one(sql, dialect="bigquery")).sql("bigquery")
        "SELECT SHA256(CONCAT(COALESCE(CAST(a AS STRING), '_sqlmesh_surrogate_key_null_'), '|', COALESCE(CAST(b AS STRING), '_sqlmesh_surrogate_key_null_'), '|', COALESCE(CAST(c AS STRING), '_sqlmesh_surrogate_key_null_'))) FROM foo"
    """
    string_fields: t.List[exp.Expr] = []
    for i, field in enumerate(fields):
        if i > 0:
            string_fields.append(exp.Literal.string("|"))
        string_fields.append(
            exp.func(
                "COALESCE",
                exp.cast(field, exp.DataType.build("text")),
                exp.Literal.string("_sqlmesh_surrogate_key_null_"),
            )
        )

    func = exp.func(
        hash_function.name,
        exp.func("CONCAT", *string_fields),
        dialect=evaluator.dialect,
    )
    if isinstance(func, exp.MD5Digest):
        func = exp.MD5(this=func.this)

    return func


@macro()
def safe_add(_: MacroEvaluator, *fields: exp.Expr) -> exp.Case:
    """Adds numbers together, substitutes nulls for 0s and only returns null if all fields are null.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "SELECT @SAFE_ADD(a, b) FROM foo"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'SELECT CASE WHEN a IS NULL AND b IS NULL THEN NULL ELSE COALESCE(a, 0) + COALESCE(b, 0) END FROM foo'
    """
    return (
        exp.Case()
        .when(exp.and_(*(field.is_(exp.null()) for field in fields)), exp.null())
        .else_(reduce(lambda a, b: a + b, [exp.func("COALESCE", field, 0) for field in fields]))  # type: ignore
    )


@macro()
def safe_sub(_: MacroEvaluator, *fields: exp.Expr) -> exp.Case:
    """Subtract numbers, substitutes nulls for 0s and only returns null if all fields are null.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "SELECT @SAFE_SUB(a, b) FROM foo"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'SELECT CASE WHEN a IS NULL AND b IS NULL THEN NULL ELSE COALESCE(a, 0) - COALESCE(b, 0) END FROM foo'
    """
    return (
        exp.Case()
        .when(exp.and_(*(field.is_(exp.null()) for field in fields)), exp.null())
        .else_(reduce(lambda a, b: a - b, [exp.func("COALESCE", field, 0) for field in fields]))  # type: ignore
    )


@macro()
def safe_div(_: MacroEvaluator, numerator: exp.Expr, denominator: exp.Expr) -> exp.Div:
    """Divides numbers, returns null if the denominator is 0.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "SELECT @SAFE_DIV(a, b) FROM foo"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'SELECT a / NULLIF(b, 0) FROM foo'
    """
    return numerator / exp.func("NULLIF", denominator, 0)


@macro()
def union(
    evaluator: MacroEvaluator,
    *args: exp.Expr,
) -> exp.Query:
    """Returns a UNION of the given tables. Only choosing columns that have the same name and type.

    Args:
        evaluator: MacroEvaluator that invoked the macro
        args: Variable arguments that can be:
            - First argument can be a condition (exp.Condition)
            - A union type ('ALL' or 'DISTINCT') as exp.Literal
            - Tables (exp.Table)

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlglot.schema import MappingSchema
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "@UNION('distinct', foo, bar)"
        >>> MacroEvaluator(schema=MappingSchema({"foo": {"a": "int", "b": "string", "c": "string"}, "bar": {"c": "string", "a": "int", "b": "int"}})).transform(parse_one(sql)).sql()
        'SELECT CAST(a AS INT) AS a, CAST(c AS TEXT) AS c FROM foo UNION SELECT CAST(a AS INT) AS a, CAST(c AS TEXT) AS c FROM bar'
        >>> sql = "@UNION(True, 'distinct', foo, bar)"
        >>> MacroEvaluator(schema=MappingSchema({"foo": {"a": "int", "b": "string", "c": "string"}, "bar": {"c": "string", "a": "int", "b": "int"}})).transform(parse_one(sql)).sql()
        'SELECT CAST(a AS INT) AS a, CAST(c AS TEXT) AS c FROM foo UNION SELECT CAST(a AS INT) AS a, CAST(c AS TEXT) AS c FROM bar'
    """

    if not args:
        raise SQLMeshError("At least one table is required for the @UNION macro.")

    arg_idx = 0
    # Check for condition
    condition = evaluator.eval_expression(args[arg_idx])
    if isinstance(condition, bool):
        arg_idx += 1
        if arg_idx >= len(args):
            raise SQLMeshError("Expected more arguments after the condition of the `@UNION` macro.")

    # Check for union type
    type_ = exp.Literal.string("ALL")
    if isinstance(args[arg_idx], exp.Literal):
        type_ = args[arg_idx]  # type: ignore
        arg_idx += 1
    kind = type_.name.upper()
    if kind not in ("ALL", "DISTINCT"):
        raise SQLMeshError(f"Invalid type '{type_}'. Expected 'ALL' or 'DISTINCT'.")

    # Remaining args should be tables
    tables = [
        exp.to_table(e.sql(evaluator.dialect), dialect=evaluator.dialect) for e in args[arg_idx:]
    ]

    columns = {
        column
        for column, _ in reduce(
            lambda a, b: a & b,  # type: ignore
            (evaluator.columns_to_types(table).items() for table in tables),
        )
    }

    projections = [
        exp.cast(column, type_, dialect=evaluator.dialect).as_(column)
        for column, type_ in evaluator.columns_to_types(tables[0]).items()
        if column in columns
    ]

    # Skip the union if condition is False
    if condition == False:
        return exp.select(*projections).from_(tables[0])

    return reduce(
        lambda a, b: a.union(b, distinct=kind == "DISTINCT"),  # type: ignore
        [exp.select(*projections).from_(t) for t in tables],
    )


@macro()
def haversine_distance(
    _: MacroEvaluator,
    lat1: exp.Expr,
    lon1: exp.Expr,
    lat2: exp.Expr,
    lon2: exp.Expr,
    unit: exp.Literal = exp.Literal.string("mi"),
) -> exp.Mul:
    """Returns the haversine distance between two points.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "SELECT @HAVERSINE_DISTANCE(driver_y, driver_x, passenger_y, passenger_x, 'mi') FROM rides"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'SELECT 7922 * ASIN(SQRT((POWER(SIN(RADIANS((passenger_y - driver_y) / 2)), 2)) + (COS(RADIANS(driver_y)) * COS(RADIANS(passenger_y)) * POWER(SIN(RADIANS((passenger_x - driver_x) / 2)), 2)))) * 1.0 FROM rides'
    """
    if unit.this == "mi":
        conversion_rate = 1.0
    elif unit.this == "km":
        conversion_rate = 1.60934
    else:
        raise SQLMeshError(f"Invalid unit '{unit}'. Expected 'mi' or 'km'.")

    return (
        2
        * 3961
        * exp.func(
            "ASIN",
            exp.func(
                "SQRT",
                exp.func("POWER", exp.func("SIN", exp.func("RADIANS", (lat2 - lat1) / 2)), 2)
                + exp.func("COS", exp.func("RADIANS", lat1))
                * exp.func("COS", exp.func("RADIANS", lat2))
                * exp.func("POWER", exp.func("SIN", exp.func("RADIANS", (lon2 - lon1) / 2)), 2),
            ),
        )
        * conversion_rate
    )


@macro()
def pivot(
    evaluator: MacroEvaluator,
    column: SQL,
    values: t.List[exp.Expr],
    alias: bool = True,
    agg: exp.Expr = exp.Literal.string("SUM"),
    cmp: exp.Expr = exp.Literal.string("="),
    prefix: exp.Expr = exp.Literal.string(""),
    suffix: exp.Expr = exp.Literal.string(""),
    then_value: SQL = SQL("1"),
    else_value: SQL = SQL("0"),
    quote: bool = True,
    distinct: bool = False,
) -> t.List[exp.Expr]:
    """Returns a list of projections as a result of pivoting the given column on the given values.

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "SELECT date_day, @PIVOT(status, ['cancelled', 'completed']) FROM rides GROUP BY 1"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'SELECT date_day, SUM(CASE WHEN status = \\'cancelled\\' THEN 1 ELSE 0 END) AS "cancelled", SUM(CASE WHEN status = \\'completed\\' THEN 1 ELSE 0 END) AS "completed" FROM rides GROUP BY 1'
        >>> sql = "SELECT @PIVOT(a, ['v'], then_value := tv, suffix := '_sfx', quote := FALSE)"
        >>> MacroEvaluator(dialect="bigquery").transform(parse_one(sql)).sql("bigquery")
        "SELECT SUM(CASE WHEN a = 'v' THEN tv ELSE 0 END) AS v_sfx"
    """
    aggregates: t.List[exp.Expr] = []
    for value in values:
        proj = f"{agg.name}("
        if distinct:
            proj += "DISTINCT "

        proj += f"CASE WHEN {column} {cmp.name} {value.sql(evaluator.dialect)} THEN {then_value} ELSE {else_value} END) "
        node: exp.Expr = evaluator.parse_one(proj)

        if alias:
            node = node.as_(
                f"{prefix.name}{value.name}{suffix.name}",
                quoted=quote,
                copy=False,
                dialect=evaluator.dialect,
            )

        aggregates.append(node)

    return aggregates


@macro("AND")
def and_(evaluator: MacroEvaluator, *expressions: t.Optional[exp.Expr]) -> exp.Condition:
    """Returns an AND statement filtering out any NULL expressions."""
    conditions = [e for e in expressions if not isinstance(e, exp.Null)]

    if not conditions:
        return exp.true()

    return exp.and_(*conditions, dialect=evaluator.dialect)


@macro("OR")
def or_(evaluator: MacroEvaluator, *expressions: t.Optional[exp.Expr]) -> exp.Condition:
    """Returns an OR statement filtering out any NULL expressions."""
    conditions = [e for e in expressions if not isinstance(e, exp.Null)]

    if not conditions:
        return exp.true()

    return exp.or_(*conditions, dialect=evaluator.dialect)


@macro("VAR")
def var(
    evaluator: MacroEvaluator, var_name: exp.Expr, default: t.Optional[exp.Expr] = None
) -> exp.Expr:
    """Returns the value of a variable or the default value if the variable is not set."""
    if not var_name.is_string:
        raise SQLMeshError(f"Invalid variable name '{var_name.sql()}'. Expected a string literal.")

    return exp.convert(evaluator.var(var_name.this, default))


@macro("BLUEPRINT_VAR")
def blueprint_var(
    evaluator: MacroEvaluator, var_name: exp.Expr, default: t.Optional[exp.Expr] = None
) -> exp.Expr:
    """Returns the value of a blueprint variable or the default value if the variable is not set."""
    if not var_name.is_string:
        raise SQLMeshError(
            f"Invalid blueprint variable name '{var_name.sql()}'. Expected a string literal."
        )

    return exp.convert(evaluator.blueprint_var(var_name.this, default))


@macro()
def deduplicate(
    evaluator: MacroEvaluator,
    relation: exp.Expr,
    partition_by: t.List[exp.Expr],
    order_by: t.List[str],
) -> exp.Query:
    """Returns a QUERY to deduplicate rows within a table

    Args:
        relation: table or CTE name to deduplicate
        partition_by: column names, or expressions to use to identify a window of rows out of which to select one as the deduplicated row
        order_by: A list of strings representing the ORDER BY clause

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlglot.schema import MappingSchema
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "@deduplicate(demo.table, [user_id, cast(timestamp as date)], ['timestamp desc', 'status asc'])"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        'SELECT * FROM demo.table QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id, CAST(timestamp AS DATE) ORDER BY timestamp DESC, status ASC) = 1'
    """
    if not isinstance(partition_by, list):
        raise SQLMeshError(
            "partition_by must be a list of columns: [<column>, cast(<column> as <type>)]"
        )

    if not isinstance(order_by, list):
        raise SQLMeshError(
            "order_by must be a list of strings, optional - nulls ordering: ['<column> <asc|desc> nulls <first|last>']"
        )

    partition_clause = exp.tuple_(*partition_by)

    order_expressions = [
        evaluator.transform(parse_one(order_item, into=exp.Ordered, dialect=evaluator.dialect))
        for order_item in order_by
    ]

    if not order_expressions:
        raise SQLMeshError(
            "order_by must be a list of strings, optional - nulls ordering: ['<column> <asc|desc> nulls <first|last>']"
        )

    order_clause = exp.Order(expressions=order_expressions)

    window_function = exp.Window(
        this=exp.RowNumber(), partition_by=partition_clause, order=order_clause
    )

    first_unique_row = window_function.eq(1)

    query = exp.select("*").from_(relation).qualify(first_unique_row)

    return query


@macro()
def date_spine(
    evaluator: MacroEvaluator,
    datepart: exp.Expr,
    start_date: exp.Expr,
    end_date: exp.Expr,
) -> exp.Select:
    """Returns a query that produces a date spine with the given datepart, and range of start_date and end_date. Useful for joining as a date lookup table.

    Args:
        datepart: The datepart to use for the date spine - day, week, month, quarter, year
        start_date: The start date for the date spine in format YYYY-MM-DD
        end_date: The end date for the date spine in format YYYY-MM-DD

    Example:
        >>> from sqlglot import parse_one
        >>> from sqlglot.schema import MappingSchema
        >>> from sqlmesh.core.macros import MacroEvaluator
        >>> sql = "@date_spine('week', '2022-01-20', '2024-12-16')"
        >>> MacroEvaluator().transform(parse_one(sql)).sql()
        "SELECT date_week FROM UNNEST(GENERATE_DATE_ARRAY(CAST(\'2022-01-20\' AS DATE), CAST(\'2024-12-16\' AS DATE), INTERVAL \'1\' WEEK)) AS _exploded(date_week)"
    """
    datepart_name = datepart.name.lower()
    if datepart_name not in ("day", "week", "month", "quarter", "year"):
        raise SQLMeshError(
            f"Invalid datepart '{datepart_name}'. Expected: 'day', 'week', 'month', 'quarter', or 'year'"
        )

    start_date_name = start_date.name
    end_date_name = end_date.name

    try:
        if start_date.is_string and end_date.is_string:
            start_date_obj = datetime.strptime(start_date_name, "%Y-%m-%d").date()
            end_date_obj = datetime.strptime(end_date_name, "%Y-%m-%d").date()
        else:
            start_date_obj = None
            end_date_obj = None
    except Exception as e:
        raise SQLMeshError(
            f"Invalid date format - start_date and end_date must be in format: YYYY-MM-DD. Error: {e}"
        )

    if start_date_obj and end_date_obj:
        if start_date_obj > end_date_obj:
            raise SQLMeshError(
                f"Invalid date range - start_date '{start_date_name}' is after end_date '{end_date_name}'."
            )

        start_date = exp.cast(start_date, "DATE")
        end_date = exp.cast(end_date, "DATE")

    if datepart_name == "quarter" and evaluator.dialect in (
        "spark",
        "spark2",
        "databricks",
        "postgres",
    ):
        date_interval = exp.Interval(this=exp.Literal.number(3), unit=exp.var("month"))
    else:
        date_interval = exp.Interval(this=exp.Literal.number(1), unit=exp.var(datepart_name))

    generate_date_array = exp.func(
        "GENERATE_DATE_ARRAY",
        start_date,
        end_date,
        date_interval,
    )

    alias_name = f"date_{datepart_name}"
    exploded = exp.alias_(exp.func("unnest", generate_date_array), "_exploded", table=[alias_name])

    return exp.select(alias_name).from_(exploded)


@macro()
def resolve_template(
    evaluator: MacroEvaluator,
    template: exp.Literal,
    mode: str = "literal",
) -> t.Union[exp.Literal, exp.Table]:
    """
    Generates either a String literal or an exp.Table representing a physical table location, based on rendering the provided template String literal.

    Note: It relies on the @this_model variable being available in the evaluation context (@this_model resolves to an exp.Table object
    representing the current physical table).
    Therefore, the @resolve_template macro must be used at creation or evaluation time and not at load time.

    Args:
        template: Template string literal. Can contain the following placeholders:
            @{catalog_name} -> replaced with the catalog of the exp.Table returned from @this_model
            @{schema_name} -> replaced with the schema of the exp.Table returned from @this_model
            @{table_name} -> replaced with the name of the exp.Table returned from @this_model
        mode: What to return.
            'literal' -> return an exp.Literal string
            'table' -> return an exp.Table

    Example:
        >>> from sqlglot import parse_one, exp
        >>> from sqlmesh.core.macros import MacroEvaluator, RuntimeStage
        >>> sql = "@resolve_template('s3://data-bucket/prod/@{catalog_name}/@{schema_name}/@{table_name}')"
        >>> evaluator = MacroEvaluator(runtime_stage=RuntimeStage.CREATING)
        >>> evaluator.locals.update({"this_model": exp.to_table("test_catalog.sqlmesh__test.test__test_model__2517971505")})
        >>> evaluator.transform(parse_one(sql)).sql()
        "'s3://data-bucket/prod/test_catalog/sqlmesh__test/test__test_model__2517971505'"
    """
    if "this_model" in evaluator.locals:
        this_model = exp.to_table(evaluator.locals["this_model"], dialect=evaluator.dialect)
        template_str: str = template.this
        result = (
            template_str.replace("@{catalog_name}", this_model.catalog)
            .replace("@{schema_name}", this_model.db)
            .replace("@{table_name}", this_model.name)
        )

        if mode.lower() == "table":
            return exp.to_table(result, dialect=evaluator.dialect)
        return exp.Literal.string(result)
    if evaluator.runtime_stage != RuntimeStage.LOADING.value:
        # only error if we are CREATING, EVALUATING or TESTING and @this_model is not present; this could indicate a bug
        # otherwise, for LOADING, it's a no-op
        raise SQLMeshError(
            "@this_model must be present in the macro evaluation context in order to use @resolve_template"
        )

    return template


def normalize_macro_name(name: str) -> str:
    """Prefix macro name with @ and upcase"""
    return f"@{name.upper()}"


for m in macro.get_registry().values():
    setattr(m, c.SQLMESH_BUILTIN, True)


def call_macro(
    func: t.Callable,
    dialect: DialectType,
    path: t.Optional[Path],
    provided_args: t.Tuple[t.Any, ...],
    provided_kwargs: t.Dict[str, t.Any],
    **optional_kwargs: t.Any,
) -> t.Any:
    # Bind the macro's actual parameters to its formal parameters
    sig = inspect.signature(func)

    if optional_kwargs:
        provided_kwargs = provided_kwargs.copy()

    for k, v in optional_kwargs.items():
        if k in sig.parameters:
            provided_kwargs[k] = v

    bound = sig.bind(*provided_args, **provided_kwargs)
    bound.apply_defaults()

    try:
        annotations = t.get_type_hints(func, localns=get_supported_types())
    except (NameError, TypeError):  # forward references aren't handled
        annotations = {}

    # If the macro is annotated, we try coerce the actual parameters to the corresponding types
    if annotations:
        for arg, value in bound.arguments.items():
            typ = annotations.get(arg)
            if not typ:
                continue

            # Changes to bound.arguments will reflect in bound.args and bound.kwargs
            # https://docs.python.org/3/library/inspect.html#inspect.BoundArguments.arguments
            param = sig.parameters[arg]
            if param.kind is inspect.Parameter.VAR_POSITIONAL:
                bound.arguments[arg] = tuple(_coerce(v, typ, dialect, path) for v in value)
            elif param.kind is inspect.Parameter.VAR_KEYWORD:
                bound.arguments[arg] = {k: _coerce(v, typ, dialect, path) for k, v in value.items()}
            else:
                bound.arguments[arg] = _coerce(value, typ, dialect, path)

    return func(*bound.args, **bound.kwargs)


def _coerce(
    expr: t.Any,
    typ: t.Any,
    dialect: DialectType,
    path: t.Optional[Path] = None,
    strict: bool = False,
) -> t.Any:
    """Coerces the given expression to the specified type on a best-effort basis."""
    base_err_msg = f"Failed to coerce expression '{expr}' to type '{typ}'."
    try:
        if typ is None or typ is t.Any or not isinstance(expr, exp.Expr):
            return expr
        base = t.get_origin(typ) or typ

        # We need to handle Union and TypeVars first since we cannot use isinstance with it
        if base in UNION_TYPES:
            for branch in t.get_args(typ):
                try:
                    return _coerce(expr, branch, dialect, path, strict=True)
                except Exception:
                    pass
            raise SQLMeshError(base_err_msg)
        if base is SQL and isinstance(expr, exp.Expr):
            return expr.sql(dialect)

        if base is t.Literal:
            if not isinstance(expr, (exp.Literal, exp.Boolean)):
                raise SQLMeshError(
                    f"{base_err_msg} Coercion to {base} requires a literal expression."
                )
            literal_type_args = t.get_args(typ)
            try:
                for literal_type_arg in literal_type_args:
                    expr_is_bool = isinstance(expr.this, bool)
                    literal_is_bool = isinstance(literal_type_arg, bool)
                    if (expr_is_bool and literal_is_bool and literal_type_arg == expr.this) or (
                        not expr_is_bool
                        and not literal_is_bool
                        and str(literal_type_arg) == str(expr.this)
                    ):
                        return type(literal_type_arg)(expr.this)
            except Exception:
                raise SQLMeshError(base_err_msg)
            raise SQLMeshError(base_err_msg)

        if isinstance(expr, base):
            return expr
        if issubclass(base, exp.Expr):
            d = Dialect.get_or_raise(dialect)
            into = base if base in d.parser_class.EXPRESSION_PARSERS else None
            if into is None:
                if isinstance(expr, exp.Literal):
                    coerced = parse_one(expr.this)
                else:
                    raise SQLMeshError(
                        f"{base_err_msg} Coercion to {base} requires a literal expression."
                    )
            else:
                coerced = parse_one(
                    expr.this if isinstance(expr, exp.Literal) else expr.sql(), into=into
                )
            if isinstance(coerced, base):
                return coerced
            raise SQLMeshError(base_err_msg)

        if base in (int, float, str) and isinstance(expr, exp.Literal):
            return base(expr.this)
        if base is str and isinstance(expr, exp.Column) and not expr.table:
            return expr.name
        if base is bool and isinstance(expr, exp.Boolean):
            return expr.this
        if base is datetime and isinstance(expr, exp.Literal):
            return to_datetime(expr.this)
        if base is date and isinstance(expr, exp.Literal):
            return to_date(expr.this)
        if base is tuple and isinstance(expr, (exp.Tuple, exp.Array)):
            generic = t.get_args(typ)
            if not generic:
                return tuple(expr.expressions)
            if generic[-1] is ...:
                return tuple(_coerce(expr, generic[0], dialect, path) for expr in expr.expressions)
            if len(generic) == len(expr.expressions):
                return tuple(
                    _coerce(expr, generic[i], dialect, path)
                    for i, expr in enumerate(expr.expressions)
                )
            raise SQLMeshError(f"{base_err_msg} Expected {len(generic)} items.")
        if base is list and isinstance(expr, (exp.Array, exp.Tuple)):
            generic = t.get_args(typ)
            if not generic:
                return expr.expressions
            return [_coerce(expr, generic[0], dialect, path) for expr in expr.expressions]
        raise SQLMeshError(base_err_msg)
    except Exception:
        if strict:
            raise

        from sqlmesh.core.console import get_console

        get_console().log_error(
            f"Coercion of expression '{expr}' to type '{typ}' failed. Using non coerced expression at '{path}'",
        )
        return expr


def convert_sql(v: t.Any, dialect: DialectType) -> t.Any:
    try:
        return _cache_convert_sql(v, dialect, v.__class__)
    # dicts aren't hashable but are convertable
    except TypeError:
        return _convert_sql(v, dialect)


def _convert_sql(v: t.Any, dialect: DialectType) -> t.Any:
    if not isinstance(v, str):
        try:
            v = exp.convert(v)
        # we use bare Exception instead of ValueError because there's
        # a recursive error with MagicMock.
        except Exception:
            pass

    if isinstance(v, exp.Expr):
        if (isinstance(v, exp.Column) and not v.table) or (
            isinstance(v, exp.Identifier) or v.is_string
        ):
            return v.name
        v = v.sql(dialect=dialect)
    return v


@lru_cache(maxsize=16384)
def _cache_convert_sql(v: t.Any, dialect: DialectType, t: type) -> t.Any:
    return _convert_sql(v, dialect)
