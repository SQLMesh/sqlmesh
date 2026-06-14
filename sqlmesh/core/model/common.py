from __future__ import annotations

import ast
import typing as t
from pathlib import Path

from astor import to_source
from difflib import get_close_matches
from sqlglot import exp
from sqlglot.helper import ensure_list

from sqlmesh.core import constants as c
from sqlmesh.core import dialect as d
from sqlmesh.core.macros import MacroRegistry, MacroStrTemplate
from sqlmesh.utils import str_to_bool
from sqlmesh.utils.errors import ConfigError, SQLMeshError, raise_config_error
from sqlmesh.utils.metaprogramming import (
    Executable,
    SqlValue,
    build_env,
    prepare_env,
    serialize_env,
)
from sqlmesh.utils.pydantic import (
    PydanticModel,
    ValidationInfo,
    field_validator,
    get_dialect,
    validation_data,
)

if t.TYPE_CHECKING:
    from sqlglot.dialects.dialect import DialectType
    from sqlmesh.utils import registry_decorator
    from sqlmesh.utils.jinja import MacroReference

    MacroCallable = t.Union[Executable, registry_decorator]


def make_python_env(
    expressions: t.Union[
        exp.Expr,
        t.List[t.Union[exp.Expr, t.Tuple[exp.Expr, bool]]],
    ],
    jinja_macro_references: t.Optional[t.Set[MacroReference]],
    module_path: Path,
    macros: MacroRegistry,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    referenced_variables: t.Optional[t.Set[str]] = None,
    path: t.Optional[Path] = None,
    python_env: t.Optional[t.Dict[str, Executable]] = None,
    strict_resolution: bool = True,
    blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
    dialect: DialectType = None,
) -> t.Dict[str, Executable]:
    python_env = {} if python_env is None else python_env
    env: t.Dict[str, t.Tuple[t.Any, t.Optional[bool]]] = {}

    variables = variables or {}
    blueprint_variables = blueprint_variables or {}

    used_macros: t.Dict[str, t.Tuple[MacroCallable, bool]] = {}

    # var -> True: var is metadata-only
    # var -> False: var is not metadata-only
    # var -> None: cannot determine whether var is metadata-only yet, need to walk macros first
    used_variables: t.Dict[str, t.Optional[bool]] = dict.fromkeys(
        referenced_variables or set(), False
    )

    # id(expr) -> true: expr appears under the AST of a metadata-only macro function
    # id(expr) -> false: expr appears under the AST of a macro function whose metadata status we don't yet know
    expr_under_metadata_macro_func: t.Dict[int, bool] = {}

    # For @m1(@m2(@x), @y), we'd get x -> m1 and y -> m1
    outermost_macro_func_ancestor_by_var: t.Dict[str, str] = {}
    visited_macro_funcs: t.Set[int] = set()

    def _is_metadata_var(
        name: str, expression: exp.Expr, appears_in_metadata_expression: bool
    ) -> t.Optional[bool]:
        is_metadata_so_far = used_variables.get(name, True)
        if is_metadata_so_far is False:
            # We've concluded this variable is definitely not metadata-only
            return False

        appears_under_metadata_macro_func = expr_under_metadata_macro_func.get(id(expression))
        if is_metadata_so_far and (
            appears_in_metadata_expression or appears_under_metadata_macro_func
        ):
            # The variable appears in a metadata expression, e.g., audits (...),
            # or in the AST of metadata-only macro call, e.g., @FOO(@x)
            return True

        # The variable appears in the AST of a macro call, but we don't know if it's metadata-only
        if appears_under_metadata_macro_func is False:
            return None

        # The variable appears elsewhere, e.g., in the model's query: SELECT @x
        return False

    def _is_metadata_macro(name: str, appears_in_metadata_expression: bool) -> bool:
        if name in used_macros:
            is_metadata_so_far = used_macros[name][1]
            return is_metadata_so_far and appears_in_metadata_expression

        return appears_in_metadata_expression

    expressions = ensure_list(expressions)
    for expression_metadata in expressions:
        if isinstance(expression_metadata, tuple):
            expression, is_metadata = expression_metadata
        else:
            expression, is_metadata = expression_metadata, False

        if isinstance(expression, d.Jinja):
            continue

        for macro_func_or_var in expression.find_all(d.MacroFunc, d.MacroVar, exp.Identifier):
            if macro_func_or_var.__class__ is d.MacroFunc:
                name = macro_func_or_var.this.name.lower()
                if name not in macros:
                    continue

                used_macros[name] = (macros[name], _is_metadata_macro(name, is_metadata))

                if name in (c.VAR, c.BLUEPRINT_VAR):
                    args = macro_func_or_var.this.expressions
                    if len(args) < 1:
                        raise_config_error(
                            f"Macro {name.upper()} requires at least one argument", path
                        )

                    if not args[0].is_string:
                        raise_config_error(
                            f"The variable name must be a string literal, '{args[0].sql()}' was given instead",
                            path,
                        )

                    var_name = args[0].this.lower()
                    used_variables[var_name] = _is_metadata_var(
                        var_name, macro_func_or_var, is_metadata
                    )
                elif id(macro_func_or_var) not in visited_macro_funcs:
                    # We only care about the top-level macro function calls to determine the metadata
                    # status of the variables referenced in their ASTs. For example, in @m1(@m2(@x)),
                    # if m1 is metadata-only but m2 is not, we can still determine that @x only affects
                    # the metadata hash, since m2's result feeds into a metadata-only macro function.
                    #
                    # Generally, if the top-level call is known to be metadata-only or appear in a
                    # metadata expression, then we can avoid traversing nested macro function calls.

                    var_refs, _expr_under_metadata_macro_func, _visited_macro_funcs = (
                        _extract_macro_func_variable_references(macro_func_or_var, is_metadata)
                    )
                    expr_under_metadata_macro_func.update(_expr_under_metadata_macro_func)
                    visited_macro_funcs.update(_visited_macro_funcs)
                    outermost_macro_func_ancestor_by_var |= {var_ref: name for var_ref in var_refs}
            elif macro_func_or_var.__class__ is d.MacroVar:
                var_name = macro_func_or_var.name.lower()
                if var_name in macros:
                    used_macros[var_name] = (
                        macros[var_name],
                        _is_metadata_macro(var_name, is_metadata),
                    )
                elif var_name in variables or var_name in blueprint_variables:
                    used_variables[var_name] = _is_metadata_var(
                        var_name, macro_func_or_var, is_metadata
                    )
            elif (
                isinstance(macro_func_or_var, (exp.Identifier, d.MacroStrReplace, d.MacroSQL))
            ) and "@" in macro_func_or_var.name:
                for _, identifier, braced_identifier, _ in MacroStrTemplate.pattern.findall(
                    macro_func_or_var.name
                ):
                    var_name = braced_identifier or identifier
                    if var_name in variables or var_name in blueprint_variables:
                        used_variables[var_name] = _is_metadata_var(
                            var_name, macro_func_or_var, is_metadata
                        )

    for macro_ref in jinja_macro_references or set():
        if macro_ref.package is None and macro_ref.name in macros:
            used_macros[macro_ref.name] = (macros[macro_ref.name], False)

    for name, (used_macro, is_metadata) in used_macros.items():
        if isinstance(used_macro, Executable):
            python_env[name] = used_macro
        elif not hasattr(used_macro, c.SQLMESH_BUILTIN) and name not in python_env:
            build_env(
                used_macro.func,
                env=env,
                name=name,
                path=module_path,
                is_metadata_obj=is_metadata,
            )

    python_env.update(serialize_env(env, path=module_path))
    return _add_variables_to_python_env(
        python_env,
        used_variables,
        variables,
        blueprint_variables=blueprint_variables,
        dialect=dialect,
        strict_resolution=strict_resolution,
        outermost_macro_func_ancestor_by_var=outermost_macro_func_ancestor_by_var,
    )


def _extract_macro_func_variable_references(
    macro_func: exp.Expr,
    is_metadata: bool,
) -> t.Tuple[t.Set[str], t.Dict[int, bool], t.Set[int]]:
    var_references = set()
    visited_macro_funcs = set()
    expr_under_metadata_macro_func = {}

    for n in macro_func.walk():
        if type(n) is d.MacroFunc:
            visited_macro_funcs.add(id(n))

            this = n.this
            args = this.expressions

            if this.name.lower() in (c.VAR, c.BLUEPRINT_VAR) and args and args[0].is_string:
                var_references.add(args[0].this.lower())
                expr_under_metadata_macro_func[id(n)] = is_metadata
        elif isinstance(n, d.MacroVar):
            var_references.add(n.name.lower())
            expr_under_metadata_macro_func[id(n)] = is_metadata
        elif isinstance(n, (exp.Identifier, d.MacroStrReplace, d.MacroSQL)) and "@" in n.name:
            var_references.update(
                (braced_identifier or identifier).lower()
                for _, identifier, braced_identifier, _ in MacroStrTemplate.pattern.findall(n.name)
            )
            expr_under_metadata_macro_func[id(n)] = is_metadata

    return (var_references, expr_under_metadata_macro_func, visited_macro_funcs)


def _add_variables_to_python_env(
    python_env: t.Dict[str, Executable],
    used_variables: t.Dict[str, t.Optional[bool]],
    variables: t.Optional[t.Dict[str, t.Any]],
    strict_resolution: bool = True,
    blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
    dialect: DialectType = None,
    outermost_macro_func_ancestor_by_var: t.Optional[t.Dict[str, str]] = None,
) -> t.Dict[str, Executable]:
    _, python_used_variables = parse_dependencies(
        python_env,
        None,
        strict_resolution=strict_resolution,
        variables=variables,
        blueprint_variables=blueprint_variables,
    )
    for var_name, is_metadata in python_used_variables.items():
        used_variables[var_name] = is_metadata and used_variables.get(var_name, True)

    # Variables are treated as metadata-only when all of their references either:
    # - appear in metadata-only expressions, such as `audits (...)`, virtual statements, etc
    # - appear in the ASTs or definitions of metadata-only macros
    #
    # See also: https://github.com/SQLMesh/sqlmesh/pull/4936#issuecomment-3136339936,
    # specifically the "Terminology" and "Observations" section.
    metadata_used_variables = {
        var_name for var_name, is_metadata in used_variables.items() if is_metadata
    }
    for used_var, outermost_macro_func in (outermost_macro_func_ancestor_by_var or {}).items():
        used_var_is_metadata = used_variables.get(used_var)
        if used_var_is_metadata is False:
            continue

        # At this point we can decide whether a variable reference in a macro call's AST is
        # metadata-only, because we've annotated the corresponding macro call in the python env.
        if outermost_macro_func in python_env and python_env[outermost_macro_func].is_metadata:
            metadata_used_variables.add(used_var)

    non_metadata_used_variables = set(used_variables) - metadata_used_variables

    if overlapping_variables := (non_metadata_used_variables & metadata_used_variables):
        raise ConfigError(
            f"Variables {', '.join(overlapping_variables)} are both metadata and non-metadata, "
            "which is unexpected. Please file an issue at https://github.com/SQLMesh/sqlmesh/issues/new."
        )

    metadata_variables = {
        k: v for k, v in (variables or {}).items() if k in metadata_used_variables
    }
    variables = {k: v for k, v in (variables or {}).items() if k in non_metadata_used_variables}

    if variables:
        python_env[c.SQLMESH_VARS] = Executable.value(variables, sort_root_dict=True)
    if metadata_variables:
        python_env[c.SQLMESH_VARS_METADATA] = Executable.value(
            metadata_variables, sort_root_dict=True, is_metadata=True
        )

    if blueprint_variables:
        metadata_blueprint_variables = {
            k: SqlValue(sql=v.sql(dialect=dialect)) if isinstance(v, exp.Expr) else v
            for k, v in blueprint_variables.items()
            if k in metadata_used_variables
        }
        blueprint_variables = {
            k.lower(): SqlValue(sql=v.sql(dialect=dialect)) if isinstance(v, exp.Expr) else v
            for k, v in blueprint_variables.items()
            if k in non_metadata_used_variables
        }
        if blueprint_variables:
            python_env[c.SQLMESH_BLUEPRINT_VARS] = Executable.value(
                blueprint_variables, sort_root_dict=True
            )
        if metadata_blueprint_variables:
            python_env[c.SQLMESH_BLUEPRINT_VARS_METADATA] = Executable.value(
                metadata_blueprint_variables, sort_root_dict=True, is_metadata=True
            )

    return python_env


def parse_dependencies(
    python_env: t.Dict[str, Executable],
    entrypoint: t.Optional[str],
    strict_resolution: bool = True,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
) -> t.Tuple[t.Set[str], t.Dict[str, bool]]:
    """
    Parses the source of a model function and finds upstream table dependencies
    and referenced variables based on calls to context / evaluator.

    Args:
        python_env: A dictionary of Python definitions.
        entrypoint: The name of the function.
        strict_resolution: If true, the arguments of `table` and `resolve_table` calls must
            be resolvable at parse time, otherwise an exception will be raised.
        variables: The variables available to the python environment.
        blueprint_variables: The blueprint variables available to the python environment.

    Returns:
        A tuple containing the set of upstream table dependencies and a mapping of
        the referenced variables associated with their metadata status.
    """

    class VariableResolutionContext:
        """This enables calls like `resolve_table` to reference `var()` and `blueprint_var()`."""

        @staticmethod
        def var(var_name: str, default: t.Optional[t.Any] = None) -> t.Optional[t.Any]:
            return (variables or {}).get(var_name.lower(), default)

        @staticmethod
        def blueprint_var(var_name: str, default: t.Optional[t.Any] = None) -> t.Optional[t.Any]:
            return (blueprint_variables or {}).get(var_name.lower(), default)

    env = prepare_env(python_env)
    local_env = dict.fromkeys(("context", "evaluator"), VariableResolutionContext)

    depends_on = set()
    used_variables: t.Dict[str, bool] = {}

    for executable in python_env.values():
        if not executable.is_definition:
            continue

        is_metadata = executable.is_metadata
        for node in ast.walk(ast.parse(executable.payload)):
            next_variables = set()

            if isinstance(node, ast.Call):
                func = node.func
                if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                    continue

                def get_first_arg(keyword_arg_name: str) -> t.Any:
                    if node.args:
                        first_arg: t.Optional[ast.expr] = node.args[0]
                    else:
                        first_arg = next(
                            (
                                keyword.value
                                for keyword in node.keywords
                                if keyword.arg == keyword_arg_name
                            ),
                            None,
                        )

                    try:
                        expression = to_source(first_arg)
                        return eval(expression, env, local_env)
                    except Exception:
                        if strict_resolution:
                            raise ConfigError(
                                f"Error resolving dependencies for '{executable.path}'. "
                                f"Argument '{expression.strip()}' must be resolvable at parse time."
                            )

                if func.value.id == "context" and func.attr in ("table", "resolve_table"):
                    depends_on.add(get_first_arg("model_name"))
                elif func.value.id in ("context", "evaluator") and func.attr in (
                    c.VAR,
                    c.BLUEPRINT_VAR,
                ):
                    next_variables.add(get_first_arg("var_name").lower())
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in ("context", "evaluator")
                and node.attr == c.GATEWAY
            ):
                # Check whether the gateway attribute is referenced.
                next_variables.add(c.GATEWAY)
            elif isinstance(node, ast.FunctionDef) and node.name == entrypoint:
                next_variables.update(
                    [
                        arg.arg
                        for arg in [*node.args.args, *node.args.kwonlyargs]
                        if arg.arg != "context"
                    ]
                )

            for var_name in next_variables:
                used_variables[var_name] = used_variables.get(var_name, True) and bool(is_metadata)

    return depends_on, used_variables


def validate_extra_and_required_fields(
    klass: t.Type[PydanticModel],
    provided_fields: t.Set[str],
    entity_name: str,
    path: t.Optional[Path] = None,
) -> None:
    missing_required_fields = klass.missing_required_fields(provided_fields)
    if missing_required_fields:
        field_names = "'" + "', '".join(missing_required_fields) + "'"
        raise_config_error(
            f"Please add required field{'s' if len(missing_required_fields) > 1 else ''} {field_names} to the {entity_name}.",
            path,
        )

    extra_fields = klass.extra_fields(provided_fields)
    if extra_fields:
        extra_field_names = "'" + "', '".join(extra_fields) + "'"

        all_fields = klass.all_fields()
        close_matches = {}
        for field in extra_fields:
            matches = get_close_matches(field, all_fields, n=1)
            if matches:
                close_matches[field] = matches[0]

        if len(close_matches) == 1:
            similar_msg = ". Did you mean " + "'" + "', '".join(close_matches.values()) + "'?"
        else:
            similar = [
                f"- {field}: Did you mean '{match}'?" for field, match in close_matches.items()
            ]
            similar_msg = "\n\n  " + "\n  ".join(similar) if similar else ""

        raise_config_error(
            f"Invalid field name{'s' if len(extra_fields) > 1 else ''} present in the {entity_name}: {extra_field_names}{similar_msg}",
            path,
        )


def single_value_or_tuple(values: t.Sequence) -> exp.Identifier | exp.Tuple:
    return (
        exp.to_identifier(values[0])
        if len(values) == 1
        else exp.Tuple(expressions=[exp.to_identifier(v) for v in values])
    )


def parse_expression(
    cls: t.Type,
    v: t.Union[t.List[str], t.List[exp.Expr], str, exp.Expr, t.Callable, None],
    info: t.Optional[ValidationInfo],
) -> t.List[exp.Expr] | exp.Expr | t.Callable | None:
    """Helper method to deserialize SQLGlot expressions in Pydantic Models."""
    if v is None:
        return None

    if callable(v):
        return v

    dialect = validation_data(info).get("dialect") if info else ""

    if isinstance(v, list):
        return [
            e if isinstance(e, exp.Expr) else d.parse_one(e, dialect=dialect)  # type: ignore[misc]
            for e in v
            if not isinstance(e, exp.Semicolon)
        ]

    if isinstance(v, str):
        return d.parse_one(v, dialect=dialect)

    if not v:
        raise ConfigError(f"Could not parse {v}")

    return v


def parse_bool(v: t.Any) -> bool:
    if isinstance(v, exp.Expr):
        if not isinstance(v, exp.Boolean):
            from sqlglot.optimizer.simplify import simplify

            # Try to reduce expressions like (1 = 1) (see: T-SQL boolean generation)
            v = simplify(v)

        if isinstance(v, exp.Boolean):
            return v.this

        return str_to_bool(v.name)

    return str_to_bool(str(v or ""))


def parse_properties(
    cls: t.Type, v: t.Any, info: t.Optional[ValidationInfo]
) -> t.Optional[exp.Tuple]:
    if v is None:
        return v

    dialect = validation_data(info).get("dialect") if info else ""

    if isinstance(v, str):
        v = d.parse_one(v, dialect=dialect)
    if isinstance(v, (exp.Array, exp.Paren, exp.Tuple)):
        eq_expressions: t.List[exp.Expr] = (
            [v.unnest()] if isinstance(v, exp.Paren) else v.expressions
        )

        for eq_expr in eq_expressions:
            if not isinstance(eq_expr, exp.EQ):
                raise ConfigError(
                    f"Invalid property '{eq_expr.sql(dialect=dialect)}'. "
                    "Properties must be specified as key-value pairs <key> = <value>. "
                )

        properties = (
            exp.Tuple(expressions=eq_expressions) if isinstance(v, (exp.Paren, exp.Array)) else v
        )
    elif isinstance(v, dict):
        properties = exp.Tuple(
            expressions=[exp.Literal.string(key).eq(value) for key, value in v.items()]
        )
    else:
        raise SQLMeshError(f"Unexpected properties '{v}'")

    properties.meta["dialect"] = dialect
    return properties


def default_catalog(cls: t.Type, v: t.Any) -> t.Optional[str]:
    if v is None:
        return None
    # If v is an expression then we will return expression as sql without a dialect
    return str(v)


def depends_on(cls: t.Type, v: t.Any, info: ValidationInfo) -> t.Optional[t.Set[str]]:
    data = validation_data(info)
    dialect = data.get("dialect")
    default_catalog = data.get("default_catalog")

    if isinstance(v, exp.Paren):
        v = v.unnest()

    if isinstance(v, (exp.Array, exp.Tuple)):
        return {
            d.normalize_model_name(
                table.name if table.is_string else table,
                default_catalog=default_catalog,
                dialect=dialect,
            )
            for table in v.expressions
        }
    if isinstance(v, (exp.Table, exp.Column)):
        return {d.normalize_model_name(v, default_catalog=default_catalog, dialect=dialect)}
    if hasattr(v, "__iter__") and not isinstance(v, str):
        return {
            d.normalize_model_name(name, default_catalog=default_catalog, dialect=dialect)
            for name in v
        }

    return v


def sort_python_env(python_env: t.Dict[str, Executable]) -> t.List[t.Tuple[str, Executable]]:
    """Returns the python env sorted."""
    return sorted(python_env.items(), key=lambda x: (x[1].kind, x[0]))


def sorted_python_env_payloads(python_env: t.Dict[str, Executable]) -> t.List[str]:
    """Returns the payloads of the sorted python env."""

    def _executable_to_str(k: str, v: Executable) -> str:
        result = f"# {v.path}\n" if v.path is not None else ""
        if v.is_import or v.is_definition:
            result += v.payload
        else:
            result += f"{k} = {v.payload}"
        return result

    return [_executable_to_str(k, v) for k, v in sort_python_env(python_env)]


def parse_strings_with_macro_refs(value: t.Any, dialect: DialectType) -> t.Any:
    if isinstance(value, str) and "@" in value:
        return exp.maybe_parse(value, dialect=dialect)

    if isinstance(value, dict):
        for k, v in dict(value).items():
            value[k] = parse_strings_with_macro_refs(v, dialect)
    elif isinstance(value, list):
        value = [parse_strings_with_macro_refs(v, dialect) for v in value]

    return value


expression_validator: t.Callable = field_validator(
    "unique_key",
    mode="before",
    check_fields=False,
)(parse_expression)


bool_validator: t.Callable = field_validator(
    "skip",
    "blocking",
    "forward_only",
    "disable_restatement",
    "insert_overwrite",
    "allow_partials",
    "enabled",
    "optimize_query",
    "formatting",
    mode="before",
    check_fields=False,
)(parse_bool)


properties_validator: t.Callable = field_validator(
    "physical_properties_",
    "virtual_properties_",
    "materialization_properties_",
    "grants_",
    mode="before",
    check_fields=False,
)(parse_properties)


default_catalog_validator: t.Callable = field_validator(
    "default_catalog",
    mode="before",
    check_fields=False,
)(default_catalog)


depends_on_validator: t.Callable = field_validator(
    "depends_on_",
    mode="before",
    check_fields=False,
)(depends_on)


class ParsableSql(PydanticModel):
    sql: str
    transaction: t.Optional[bool] = None

    _parsed: t.Optional[exp.Expr] = None
    _parsed_dialect: t.Optional[str] = None

    def parse(self, dialect: str) -> exp.Expr:
        if self._parsed is None or self._parsed_dialect != dialect:
            self._parsed = d.parse_one(self.sql, dialect=dialect)
            self._parsed_dialect = dialect
        return self._parsed  # type: ignore[return-value]

    @classmethod
    def from_parsed_expression(
        cls, parsed_expression: exp.Expr, dialect: str, use_meta_sql: bool = False
    ) -> ParsableSql:
        sql = (
            parsed_expression.meta.get("sql") or parsed_expression.sql(dialect=dialect)
            if use_meta_sql
            else parsed_expression.sql(dialect=dialect)
        )
        result = cls(sql=sql)
        result._parsed = parsed_expression
        result._parsed_dialect = dialect
        return result

    @classmethod
    def validator(cls) -> classmethod:
        def _validate_parsable_sql(
            v: t.Any, info: ValidationInfo
        ) -> t.Optional[t.Union[ParsableSql, t.List[ParsableSql]]]:
            if v is None:
                return v
            if isinstance(v, str):
                return ParsableSql(sql=v)
            if isinstance(v, exp.Expr):
                return ParsableSql.from_parsed_expression(
                    v, get_dialect(info.data), use_meta_sql=False
                )
            if isinstance(v, list):
                dialect = get_dialect(info.data)
                return [
                    ParsableSql(sql=s)
                    if isinstance(s, str)
                    else ParsableSql.from_parsed_expression(s, dialect, use_meta_sql=False)
                    if isinstance(s, exp.Expr)
                    else ParsableSql.parse_obj(s)
                    for s in v
                ]
            return ParsableSql.parse_obj(v)

        return field_validator(
            "query_",
            "expressions_",
            "pre_statements_",
            "post_statements_",
            "on_virtual_update_",
            mode="before",
            check_fields=False,
        )(_validate_parsable_sql)
