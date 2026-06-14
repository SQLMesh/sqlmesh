from __future__ import annotations

import importlib
import json
import re
import typing as t
import zlib
from collections import defaultdict
from enum import Enum
from sys import exc_info
from traceback import walk_tb

from jinja2 import Environment, Template, nodes, UndefinedError
from jinja2.runtime import Macro
from sqlglot import Dialect, Parser, TokenType
from sqlglot.expressions import Expression

from sqlmesh.core import constants as c
from sqlmesh.core import dialect as d
from sqlmesh.utils import AttributeDict
from sqlmesh.utils.pydantic import PRIVATE_FIELDS, PydanticModel, field_serializer, field_validator
from sqlmesh.utils.metaprogramming import SqlValue


if t.TYPE_CHECKING:
    CallNames = t.Tuple[t.Tuple[str, ...], t.Union[nodes.Call, nodes.Getattr]]

SQLMESH_JINJA_PACKAGE = "sqlmesh.utils.jinja"


def environment(**kwargs: t.Any) -> Environment:
    extensions = kwargs.pop("extensions", [])
    extensions.append("jinja2.ext.do")
    extensions.append("jinja2.ext.loopcontrols")
    return Environment(extensions=extensions, **kwargs)


ENVIRONMENT = environment()


class MacroReference(PydanticModel, frozen=True):
    package: t.Optional[str] = None
    name: str

    @property
    def reference(self) -> str:
        if self.package is None:
            return self.name
        return ".".join((self.package, self.name))

    def __str__(self) -> str:
        return self.reference


class MacroInfo(PydanticModel):
    """Class to hold macro and its calls"""

    definition: str
    depends_on: t.List[MacroReference]
    is_top_level: bool = False


class MacroReturnVal(Exception):
    def __init__(self, val: t.Any):
        self.value = val


class MacroExtractor(Parser):
    def extract(self, jinja: str, dialect: str = "") -> t.Dict[str, MacroInfo]:
        """Extract a dictionary of macro definitions from a jinja string.

        Args:
            jinja: The jinja string to extract from.
            dialect: The dialect of SQL.

        Returns:
            A dictionary of macro name to macro definition.
        """
        self.reset()
        self.sql = jinja
        self._tokens = Dialect.get_or_raise(dialect).tokenize(jinja)

        # guard for older sqlglot versions (before 30.0.3)
        if hasattr(self, "_tokens_size"):
            # keep the cached length in sync
            self._tokens_size = len(self._tokens)
        self._index = -1
        self._advance()

        macros: t.Dict[str, MacroInfo] = {}

        while self._curr:
            if self._curr.token_type == TokenType.BLOCK_START:
                macro_start = self._curr
            elif self._tag == "MACRO" and self._next:
                name = self._next.text
                while self._curr and self._curr.token_type != TokenType.BLOCK_END:
                    self._advance()

                while self._curr and self._tag != "ENDMACRO":
                    self._advance()

                macro_str = self._find_sql(macro_start, self._next)
                macros[name] = MacroInfo(
                    definition=macro_str,
                    depends_on=list(extract_macro_references_and_variables(macro_str)[0]),
                )

            self._advance()

        return macros

    def _advance(self, times: int = 1) -> None:
        super()._advance(times)
        self._tag = (
            self._curr.text.upper()
            if self._curr and self._prev and self._prev.token_type == TokenType.BLOCK_START
            else ""
        )


def call_name(node: nodes.Expr) -> t.Tuple[str, ...]:
    if isinstance(node, nodes.Name):
        return (node.name,)
    if isinstance(node, nodes.Const):
        return (f"'{node.value}'",)
    if isinstance(node, nodes.Getattr):
        return call_name(node.node) + (node.attr,)
    if isinstance(node, (nodes.Getitem, nodes.Call)):
        return call_name(node.node)
    return ()


def render_jinja(query: str, methods: t.Optional[t.Dict[str, t.Any]] = None) -> str:
    return ENVIRONMENT.from_string(query).render(methods or {})


def find_call_names(node: nodes.Node, vars_in_scope: t.Set[str]) -> t.Iterator[CallNames]:
    vars_in_scope = vars_in_scope.copy()
    for child_node in node.iter_child_nodes():
        if "target" in child_node.fields:
            # For nodes with assignment targets (Assign, AssignBlock, For, Import),
            # the target name could shadow a reference in the right hand side.
            # So we need to process the RHS before adding the target to scope.
            # For example: {% set model = model.path %} should track model.path.
            yield from find_call_names(child_node, vars_in_scope)

            target = getattr(child_node, "target")
            if isinstance(target, nodes.Name):
                vars_in_scope.add(target.name)
            elif isinstance(target, nodes.Tuple):
                for item in target.items:
                    if isinstance(item, nodes.Name):
                        vars_in_scope.add(item.name)
        elif isinstance(child_node, nodes.Macro):
            for arg in child_node.args:
                vars_in_scope.add(arg.name)
        elif isinstance(child_node, nodes.Call) or (
            isinstance(child_node, nodes.Getattr) and not isinstance(child_node.node, nodes.Getattr)
        ):
            name = call_name(child_node)
            if name[0][0] != "'" and name[0] not in vars_in_scope:
                yield (name, child_node)

        if "target" not in child_node.fields:
            yield from find_call_names(child_node, vars_in_scope)


def extract_call_names(
    jinja_str: str, cache: t.Optional[t.Dict[str, t.Tuple[t.List[CallNames], bool]]] = None
) -> t.List[CallNames]:
    def parse() -> t.List[CallNames]:
        return list(find_call_names(ENVIRONMENT.parse(jinja_str), set()))

    if cache is not None:
        key = str(zlib.crc32(jinja_str.encode("utf-8")))
        if key in cache:
            names = cache[key][0]
        else:
            names = parse()
        cache[key] = (names, True)
        return names
    return parse()


def is_variable_node(n: nodes.Node) -> bool:
    return (
        isinstance(n, nodes.Call)
        and isinstance(n.node, nodes.Name)
        and n.node.name in (c.VAR, c.BLUEPRINT_VAR)
    )


def extract_macro_references_and_variables(
    *jinja_strs: str,
) -> t.Tuple[t.Set[MacroReference], t.Set[str]]:
    macro_references = set()
    variables = set()
    for jinja_str in jinja_strs:
        for call_name, node in extract_call_names(jinja_str):
            if call_name[0] in (c.VAR, c.BLUEPRINT_VAR):
                if not is_variable_node(node):
                    # Find the variable node which could be nested
                    for n in node.find_all(nodes.Call):
                        if is_variable_node(n):
                            node = n
                            break
                    else:
                        raise ValueError(f"Could not find variable name in {jinja_str}")
                node = t.cast(nodes.Call, node)
                args = [jinja_call_arg_name(arg) for arg in node.args]
                if args and args[0]:
                    variables.add(args[0].lower())
            elif call_name[0] == c.GATEWAY:
                variables.add(c.GATEWAY)
            elif len(call_name) == 1:
                macro_references.add(MacroReference(name=call_name[0]))
            elif len(call_name) == 2:
                macro_references.add(MacroReference(package=call_name[0], name=call_name[1]))
    return macro_references, variables


def sort_dict_recursive(
    item: t.Dict[str, t.Any],
) -> t.Dict[str, t.Any]:
    sorted_dict: t.Dict[str, t.Any] = {}
    for k, v in sorted(item.items()):
        if isinstance(v, list):
            sorted_dict[k] = sorted(v)
        elif isinstance(v, dict):
            sorted_dict[k] = sort_dict_recursive(v)
        else:
            sorted_dict[k] = v
    return sorted_dict


JinjaGlobalAttribute = t.Union[str, int, float, bool, AttributeDict]


class JinjaMacroRegistry(PydanticModel):
    """Registry for Jinja macros.

    Args:
        packages: The mapping from package name to a collection of macro definitions.
        root_macros: The collection of top-level macro definitions.
        global_objs: The global objects.
        create_builtins_module: The name of a module which defines the `create_builtins` factory
            function that will be used to construct builtin variables and functions.
        root_package_name: The name of the root package. If specified root macros will be available
            as both `root_package_name.macro_name` and `macro_name`.
        top_level_packages: The list of top-level packages. Macros in this packages will be available
            as both `package_name.macro_name` and `macro_name`.
    """

    packages: t.Dict[str, t.Dict[str, MacroInfo]] = {}
    root_macros: t.Dict[str, MacroInfo] = {}
    global_objs: t.Dict[str, JinjaGlobalAttribute] = {}
    create_builtins_module: t.Optional[str] = SQLMESH_JINJA_PACKAGE
    root_package_name: t.Optional[str] = None
    top_level_packages: t.List[str] = []

    _parser_cache: t.Dict[t.Tuple[t.Optional[str], str], Template] = {}
    _trimmed: bool = False
    __environment: t.Optional[Environment] = None

    def __getstate__(self) -> t.Dict[t.Any, t.Any]:
        state = super().__getstate__()
        private = state[PRIVATE_FIELDS]
        private["_parser_cache"] = {}
        private["_JinjaMacroRegistry__environment"] = None
        return state

    @field_validator("global_objs", mode="before")
    @classmethod
    def _validate_global_objs(cls, value: t.Any) -> t.Any:
        def _normalize(val: t.Any) -> t.Any:
            if isinstance(val, dict):
                return AttributeDict({k: _normalize(v) for k, v in val.items()})
            if isinstance(val, list):
                return [_normalize(v) for v in val]
            if isinstance(val, set):
                return [_normalize(v) for v in sorted(val)]
            if isinstance(val, Enum):
                return val.value
            return val

        return _normalize(value)

    @field_serializer("global_objs")
    def _serialize_attribute_dict(
        self, value: t.Dict[str, JinjaGlobalAttribute]
    ) -> t.Dict[str, t.Any]:
        # NOTE: This is called only when used with Pydantic V2.
        def _convert(
            val: t.Union[t.Dict[str, JinjaGlobalAttribute], t.Dict[str, t.Any]],
        ) -> t.Dict[str, t.Any]:
            return {k: _convert(v) if isinstance(v, AttributeDict) else v for k, v in val.items()}

        return _convert(value)

    @property
    def trimmed(self) -> bool:
        return self._trimmed

    def add_macros(self, macros: t.Dict[str, MacroInfo], package: t.Optional[str] = None) -> None:
        """Adds macros to the target package.

        Args:
            macros: Macros that should be added.
            package: The name of the package the given macros belong to. If not specified, the provided
            macros will be added to the root namespace.
        """

        if package is not None:
            package_macros = self.packages.get(package, {})
            package_macros.update(macros)
            self.packages[package] = package_macros
        else:
            self.root_macros.update(macros)

    def add_globals(self, globals: t.Dict[str, JinjaGlobalAttribute]) -> None:
        """Adds global objects to the registry.

        Args:
            globals: The global objects that should be added.
        """
        # Keep the registry lightweight when the graph is not needed
        if not "graph" in self.packages:
            globals.pop("flat_graph", None)
        self.global_objs.update(**self._validate_global_objs(globals))

    def build_macro(self, reference: MacroReference, **kwargs: t.Any) -> t.Optional[t.Callable]:
        """Builds a Python callable for a macro with the given reference.

        Args:
            reference: The macro reference.
        Returns:
            The macro as a Python callable or None if not found.
        """
        env: Environment = self.build_environment(**kwargs)
        if reference.package is not None:
            package = env.globals.get(reference.package, {})
            return package.get(reference.name)  # type: ignore
        return env.globals.get(reference.name)  # type: ignore

    def build_environment(self, **kwargs: t.Any) -> Environment:
        """Builds a new Jinja environment based on this registry."""

        context: t.Dict[str, t.Any] = {}

        root_macros = {
            name: self._MacroWrapper(name, None, self, context)
            for name, macro in self.root_macros.items()
        }

        package_macros: t.Dict[str, t.Any] = defaultdict(AttributeDict)
        for package_name, macros in self.packages.items():
            for macro_name, macro in macros.items():
                macro_wrapper = self._MacroWrapper(macro_name, package_name, self, context)
                package_macros[package_name][macro_name] = macro_wrapper
                if macro.is_top_level and macro_name not in root_macros:
                    root_macros[macro_name] = macro_wrapper

        top_level_packages = self.top_level_packages.copy()

        if self.root_package_name is not None:
            package_macros[self.root_package_name].update(root_macros)
            top_level_packages.append(self.root_package_name)

        env = environment()

        builtin_globals = self._create_builtin_globals(kwargs)
        for top_level_package_name in top_level_packages:
            # Make sure that the top-level package doesn't fully override the same builtin package.
            package_macros[top_level_package_name] = AttributeDict(
                {
                    **(builtin_globals.pop(top_level_package_name, None) or {}),
                    **(package_macros.get(top_level_package_name) or {}),
                }
            )
            root_macros.update(package_macros[top_level_package_name])

        context.update(builtin_globals)
        context.update(root_macros)
        context.update(package_macros)
        context["render"] = lambda input: env.from_string(input).render()

        env.globals.update(context)
        env.filters.update(self._environment.filters)
        return env

    def trim(
        self, dependencies: t.Iterable[MacroReference], package: t.Optional[str] = None
    ) -> JinjaMacroRegistry:
        """Trims the registry by keeping only macros with given references and their transitive dependencies.

        Args:
            dependencies: References to macros that should be kept.
            package: The name of the package in the context of which the trimming should be performed.

        Returns:
            A new trimmed registry.
        """
        dependencies_by_package: t.Dict[t.Optional[str], t.Set[str]] = defaultdict(set)
        for dep in dependencies:
            dependencies_by_package[dep.package or package].add(dep.name)

        top_level_packages = self.top_level_packages.copy()
        if package is not None:
            top_level_packages.append(package)

        result = JinjaMacroRegistry(
            global_objs=self.global_objs.copy(),
            create_builtins_module=self.create_builtins_module,
            root_package_name=self.root_package_name,
            top_level_packages=top_level_packages,
        )
        for package, names in dependencies_by_package.items():
            result = result.merge(self._trim_macros(names, package))

        result._trimmed = True

        return result

    def merge(self, other: JinjaMacroRegistry) -> JinjaMacroRegistry:
        """Returns a copy of the registry which contains macros from both this and `other` instances.

        Args:
            other: The other registry instance.

        Returns:
            A new merged registry.
        """

        root_macros = {
            **self.root_macros,
            **other.root_macros,
        }

        packages = {}
        for package in {*self.packages, *other.packages}:
            packages[package] = {
                **self.packages.get(package, {}),
                **other.packages.get(package, {}),
            }

        global_objs = {
            **self.global_objs,
            **other.global_objs,
        }

        return JinjaMacroRegistry(
            packages=packages,
            root_macros=root_macros,
            global_objs=global_objs,
            create_builtins_module=self.create_builtins_module or other.create_builtins_module,
            root_package_name=self.root_package_name or other.root_package_name,
            top_level_packages=[*self.top_level_packages, *other.top_level_packages],
        )

    def to_expressions(self) -> t.List[Expression]:
        output: t.List[Expression] = []

        filtered_objs = {
            k: v for k, v in self.global_objs.items() if k in ("refs", "sources", "vars")
        }
        if filtered_objs:
            output.append(
                d.PythonCode(
                    expressions=[
                        f"{k} = '{v}'" if isinstance(v, str) else f"{k} = {v}"
                        for k, v in sort_dict_recursive(filtered_objs).items()
                    ]
                )
            )

        for macro_name, macro_info in sorted(self.root_macros.items()):
            output.append(d.jinja_statement(macro_info.definition))

        for _, package in sorted(self.packages.items()):
            for macro_name, macro_info in sorted(package.items()):
                output.append(d.jinja_statement(macro_info.definition))

        return output

    @property
    def data_hash_values(self) -> t.List[str]:
        data = []

        for macro_name, macro in sorted(self.root_macros.items()):
            data.append(macro_name)
            data.append(macro.definition)

        for _, package in sorted(self.packages.items()):
            for macro_name, macro in sorted(package.items()):
                data.append(macro_name)
                data.append(macro.definition)

        trimmed_global_objs = {
            k: self.global_objs[k] for k in ("refs", "sources", "vars") if k in self.global_objs
        }
        data.append(json.dumps(trimmed_global_objs, sort_keys=True))

        return data

    def __deepcopy__(self, memo: t.Optional[t.Dict[int, t.Any]] = None) -> JinjaMacroRegistry:
        return JinjaMacroRegistry.parse_obj(self.dict())

    def _parse_macro(self, name: str, package: t.Optional[str]) -> Template:
        cache_key = (package, name)
        if cache_key not in self._parser_cache:
            macro = self._get_macro(name, package)

            definition: nodes.Template = self._environment.parse(macro.definition)
            if _is_private_macro(name):
                # A workaround to expose private jinja macros.
                definition = self._to_non_private_macro_def(name, definition)

            self._parser_cache[cache_key] = self._environment.from_string(definition)
        return self._parser_cache[cache_key]

    @property
    def _environment(self) -> Environment:
        if self.__environment is None:
            self.__environment = environment()
            self.__environment.filters.update(self._create_builtin_filters())
        return self.__environment

    def _trim_macros(
        self,
        names: t.Set[str],
        package: t.Optional[str] = None,
        visited: t.Optional[t.Dict[t.Optional[str], t.Set[str]]] = None,
    ) -> JinjaMacroRegistry:
        if visited is None:
            visited = defaultdict(set)

        macros = self.packages.get(package, {}) if package is not None else self.root_macros
        trimmed_macros = {}

        dependencies: t.Dict[t.Optional[str], t.Set[str]] = defaultdict(set)

        for name in names:
            if name in macros and name not in visited[package]:
                macro = macros[name]
                trimmed_macros[name] = macro
                for dependency in macro.depends_on:
                    dependencies[dependency.package or package].add(dependency.name)
                visited[package].add(name)

        if package is not None:
            result = JinjaMacroRegistry(packages={package: trimmed_macros})
        else:
            result = JinjaMacroRegistry(root_macros=trimmed_macros)

        for upstream_package, upstream_names in dependencies.items():
            result = result.merge(
                self._trim_macros(upstream_names, upstream_package, visited=visited)
            )

        return result

    def _macro_exists(self, name: str, package: t.Optional[str]) -> bool:
        return (
            name in self.packages.get(package, {})
            if package is not None
            else name in self.root_macros
        )

    def _get_macro(self, name: str, package: t.Optional[str]) -> MacroInfo:
        return self.packages[package][name] if package is not None else self.root_macros[name]

    def _to_non_private_macro_def(self, name: str, template: nodes.Template) -> nodes.Template:
        for node in template.find_all((nodes.Macro, nodes.Call)):
            if isinstance(node, nodes.Macro):
                node.name = _non_private_name(name)
            elif isinstance(node, nodes.Call) and isinstance(node.node, nodes.Name):
                node.node.name = _non_private_name(name)

        return template

    def _create_builtin_globals(self, global_vars: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
        """Creates Jinja builtin globals using a factory function defined in the provided module."""
        engine_adapter = global_vars.pop("engine_adapter", None)
        global_vars = {**self.global_objs, **global_vars}
        if self.create_builtins_module is not None:
            module = importlib.import_module(self.create_builtins_module)
            if hasattr(module, "create_builtin_globals"):
                return module.create_builtin_globals(self, global_vars, engine_adapter)
        return global_vars

    def _create_builtin_filters(self) -> t.Dict[str, t.Any]:
        """Creates Jinja builtin filters using a factory function defined in the provided module."""
        if self.create_builtins_module is not None:
            module = importlib.import_module(self.create_builtins_module)
            if hasattr(module, "create_builtin_filters"):
                return module.create_builtin_filters()
        return {}

    class _MacroWrapper:
        def __init__(
            self,
            name: str,
            package: t.Optional[str],
            registry: JinjaMacroRegistry,
            context: t.Dict[str, t.Any],
        ):
            self.name = name
            self.package = package
            self.context = context
            self.registry = registry

        def __call__(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
            context = self.context.copy()
            if self.package is not None and self.package in context:
                context.update(context[self.package])

            template = self.registry._parse_macro(self.name, self.package)
            macro_callable = getattr(
                template.make_module(vars=context), _non_private_name(self.name)
            )
            try:
                return macro_callable(*args, **kwargs)
            except MacroReturnVal as ret:
                return ret.value


def _is_private_macro(name: str) -> bool:
    return name.startswith("_")


def _non_private_name(name: str) -> str:
    return name.lstrip("_")


JINJA_REGEX = re.compile(r"({{|{%)")


def has_jinja(value: str) -> bool:
    return JINJA_REGEX.search(value) is not None


def jinja_call_arg_name(node: nodes.Node) -> str:
    if isinstance(node, nodes.Const):
        return node.value
    return ""


def create_var(variables: t.Dict[str, t.Any]) -> t.Callable:
    def _var(var_name: str, default: t.Optional[t.Any] = None) -> t.Optional[t.Any]:
        value = variables.get(var_name.lower(), default)
        if isinstance(value, SqlValue):
            return value.sql
        return value

    return _var


def create_builtin_globals(
    jinja_macros: JinjaMacroRegistry, global_vars: t.Dict[str, t.Any], *args: t.Any, **kwargs: t.Any
) -> t.Dict[str, t.Any]:
    global_vars.pop(c.GATEWAY, None)
    variables = global_vars.pop(c.SQLMESH_VARS, None) or {}
    blueprint_variables = global_vars.pop(c.SQLMESH_BLUEPRINT_VARS, None) or {}
    return {
        **global_vars,
        c.VAR: create_var(variables),
        c.GATEWAY: lambda: variables.get(c.GATEWAY, None),
        c.BLUEPRINT_VAR: create_var(blueprint_variables),
    }


def make_jinja_registry(
    jinja_macros: JinjaMacroRegistry, package_name: str, jinja_references: t.Set[MacroReference]
) -> JinjaMacroRegistry:
    """
    Creates a Jinja macro registry for a specific package.

    This function takes an existing Jinja macro registry and returns a new
    registry that includes only the macros associated with the specified
    package and trims the registry to include only the macros referenced
    in the provided set of macro references.

    Args:
        jinja_macros: The original Jinja macro registry containing all macros.
        package_name: The name of the package for which to create the registry.
        jinja_references: A set of macro references to retain in the new registry.

    Returns:
        A new JinjaMacroRegistry containing only the macros for the specified
        package and the referenced macros.
    """

    jinja_registry = jinja_macros.copy()
    jinja_registry.root_macros = jinja_registry.packages.get(package_name) or {}
    jinja_registry = jinja_registry.trim(jinja_references)

    return jinja_registry


def extract_error_details(ex: Exception) -> str:
    """Extracts a readable message from a Jinja2 error, to include missing name and macro."""

    error_details = ""
    if isinstance(ex, UndefinedError):
        if match := re.search(r"'(\w+)'", str(ex)):
            error_details += f"\nUndefined macro/variable: '{match.group(1)}'"
        try:
            _, _, exc_traceback = exc_info()
            for frame, _ in walk_tb(exc_traceback):
                if frame.f_code.co_name == "_invoke":
                    macro = frame.f_locals.get("self")
                    if isinstance(macro, Macro):
                        error_details += f" in macro: '{macro.name}'\n"
                        break
        except:
            # to fall back to the generic error message if frame analysis fails
            pass
    return error_details or str(ex)
