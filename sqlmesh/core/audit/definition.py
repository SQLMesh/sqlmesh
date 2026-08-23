from __future__ import annotations

import pathlib
import typing as t
from functools import cached_property
from pathlib import Path

from pydantic import Field
from sqlglot import exp
from sqlglot.optimizer.simplify import gen

from sqlmesh.core import dialect as d
from sqlmesh.core.macros import MacroRegistry, macro
from sqlmesh.core.model.common import (
    bool_validator,
    default_catalog_validator,
    depends_on_validator,
    sort_python_env,
    sorted_python_env_payloads,
)
from sqlmesh.core.model.common import make_python_env, single_value_or_tuple, ParsableSql
from sqlmesh.core.node import _Node, DbtInfoMixin, DbtNodeInfo
from sqlmesh.core.renderer import QueryRenderer
from sqlmesh.utils.date import TimeLike
from sqlmesh.utils.errors import AuditConfigError, SQLMeshError, raise_config_error
from sqlmesh.utils.hashing import hash_data
from sqlmesh.utils.jinja import (
    JinjaMacroRegistry,
    extract_macro_references_and_variables,
)
from sqlmesh.utils.metaprogramming import Executable
from sqlmesh.utils.pydantic import PydanticModel, field_validator, model_validator

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import Self
    from sqlmesh.core.snapshot import DeployabilityIndex, Node, Snapshot


class AuditCommonMetaMixin:
    """
    Metadata for audits which can be defined in SQL.

    Args:
        name: The unique name of the audit.
        dialect: The dialect of the audit query.
        skip: Setting this to `true` will cause this audit to be skipped. Defaults to `false`.
        blocking: Setting this to `true` will cause the pipeline execution to stop if this audit fails.
        standalone: Setting this to `true` will cause this audit to be executed as a standalone audit.
    """

    name: str
    dialect: str
    skip: bool
    blocking: bool
    standalone: bool


class AuditMixin(AuditCommonMetaMixin):
    """
    Mixin for common Audit functionality

    Args:
        query: The audit query.
        defaults: Default values for the audit query.
        expressions: Additional sql statements to execute alongside the audit.
        jinja_macros: A registry of jinja macros to use when rendering the audit query.
    """

    query_: ParsableSql
    defaults: t.Dict[str, exp.Expr]
    expressions_: t.Optional[t.List[ParsableSql]]
    jinja_macros: JinjaMacroRegistry
    formatting: t.Optional[bool]

    @property
    def query(self) -> t.Union[exp.Query, d.JinjaQuery]:
        return t.cast(t.Union[exp.Query, d.JinjaQuery], self.query_.parse(self.dialect))

    @property
    def expressions(self) -> t.List[exp.Expr]:
        if not self.expressions_:
            return []
        result: t.List[exp.Expr] = []
        for e in self.expressions_:
            parsed = e.parse(self.dialect)
            if not isinstance(parsed, exp.Semicolon):
                result.append(parsed)
        return result

    @property
    def macro_definitions(self) -> t.List[d.MacroDef]:
        """All macro definitions from the list of expressions."""
        return [s for s in self.expressions if isinstance(s, d.MacroDef)]


@field_validator("name", "dialect", mode="before", check_fields=False)
def audit_string_validator(cls: t.Type, v: t.Any) -> t.Optional[str]:
    if isinstance(v, exp.Expr):
        return v.name.lower()
    return str(v).lower() if v is not None else None


@field_validator("defaults", mode="before", check_fields=False)
def audit_map_validator(cls: t.Type, v: t.Any, values: t.Any) -> t.Dict[str, t.Any]:
    from sqlmesh.utils.pydantic import get_dialect

    if isinstance(v, exp.Paren):
        return dict([_maybe_parse_arg_pair(v.unnest())])
    if isinstance(v, (exp.Tuple, exp.Array)):
        return dict(map(_maybe_parse_arg_pair, v.expressions))
    if isinstance(v, dict):
        dialect = get_dialect(values)
        return {
            key: value if isinstance(value, exp.Expr) else d.parse_one(str(value), dialect=dialect)
            for key, value in v.items()
        }
    raise_config_error("Defaults must be a tuple of exp.EQ or a dict", error_type=AuditConfigError)
    return {}


class ModelAudit(PydanticModel, AuditMixin, DbtInfoMixin, frozen=True):
    """
    Audit is an assertion made about your tables.

    An audit is a SQL query that returns bad records.
    """

    name: str
    dialect: str = ""
    skip: bool = False
    blocking: bool = True
    standalone: t.Literal[False] = False
    query_: ParsableSql = Field(alias="query")
    defaults: t.Dict[str, exp.Expr] = {}
    expressions_: t.Optional[t.List[ParsableSql]] = Field(default=None, alias="expressions")
    jinja_macros: JinjaMacroRegistry = JinjaMacroRegistry()
    formatting: t.Optional[bool] = Field(default=None, exclude=True)
    dbt_node_info_: t.Optional[DbtNodeInfo] = Field(alias="dbt_node_info", default=None)

    _path: t.Optional[Path] = None

    # Validators
    _query_validator = ParsableSql.validator()
    _bool_validator = bool_validator
    _string_validator = audit_string_validator
    _map_validator = audit_map_validator

    def __str__(self) -> str:
        path = f": {self._path.name}" if self._path else ""
        return f"{self.__class__.__name__}<{self.name}{path}>"

    @property
    def dbt_node_info(self) -> t.Optional[DbtNodeInfo]:
        return self.dbt_node_info_


class StandaloneAudit(_Node, AuditMixin):
    """
    Args:
        depends_on: A list of tables this audit depends on.
        python_env: Dictionary containing all global variables needed to render the audit's macros.
    """

    name: str
    dialect: str = ""
    skip: bool = False
    blocking: bool = False
    standalone: t.Literal[True] = True
    query_: ParsableSql = Field(alias="query")
    defaults: t.Dict[str, exp.Expr] = {}
    expressions_: t.Optional[t.List[ParsableSql]] = Field(default=None, alias="expressions")
    jinja_macros: JinjaMacroRegistry = JinjaMacroRegistry()
    default_catalog: t.Optional[str] = None
    depends_on_: t.Optional[t.Set[str]] = Field(default=None, alias="depends_on")
    python_env: t.Dict[str, Executable] = {}
    formatting: t.Optional[bool] = Field(default=None, exclude=True)

    source_type: t.Literal["audit"] = "audit"

    # Validators
    _query_validator = ParsableSql.validator()
    _bool_validator = bool_validator
    _string_validator = audit_string_validator
    _map_validator = audit_map_validator
    _default_catalog_validator = default_catalog_validator
    _depends_on_validator = depends_on_validator

    @model_validator(mode="after")
    def _node_root_validator(self) -> Self:
        if self.blocking:
            raise AuditConfigError(f"Standalone audits cannot be blocking: '{self.name}'.")
        return self

    def render_audit_query(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        **kwargs: t.Any,
    ) -> exp.Query:
        """Renders the audit's query.

        Args:
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time.
            snapshots: All snapshots (by name) to use for mapping of physical locations.
            deployability_index: Determines snapshots that are deployable in the context of this render.
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            The rendered expression.
        """
        query_renderer = QueryRenderer(
            self.query,
            self.dialect,
            self.macro_definitions,
            path=self._path or Path(),
            jinja_macro_registry=self.jinja_macros,
            python_env=self.python_env,
            default_catalog=self.default_catalog,
        )

        rendered_query = query_renderer.render(
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            deployability_index=deployability_index,
            **{**self.defaults, **kwargs},  # type: ignore
        )

        if rendered_query is None:
            raise SQLMeshError(f"Failed to render query for audit '{self.name}'.")

        return rendered_query

    @cached_property
    def depends_on(self) -> t.Set[str]:
        depends_on = self.depends_on_ or set()

        query = self.render_audit_query()
        if query is not None:
            depends_on |= d.find_tables(
                query, default_catalog=self.default_catalog, dialect=self.dialect
            )

        depends_on -= {self.name}
        return depends_on

    @property
    def sorted_python_env(self) -> t.List[t.Tuple[str, Executable]]:
        """Returns the python env sorted by executable kind and then var name."""
        return sort_python_env(self.python_env)

    @property
    def data_hash(self) -> str:
        """
        Computes the data hash for the node.

        Returns:
            The data hash for the node.
        """
        # StandaloneAudits do not have a data hash
        return hash_data("")

    @property
    def metadata_hash(self) -> str:
        """
        Computes the metadata hash for the node.

        Args:
            audits: Available audits by name.

        Returns:
            The metadata hash for the node.
        """
        if self._metadata_hash is None:
            data = [
                self.owner,
                self.description,
                *sorted(self.tags),
                str(self.sorted_python_env),
                self.stamp,
                self.cron,
                self.cron_tz.key if self.cron_tz else None,
            ]

            data.append(self.query_.sql)
            data.extend([e.sql for e in self.expressions_ or []])
            self._metadata_hash = hash_data(data)
        return self._metadata_hash

    def text_diff(self, other: Node, rendered: bool = False) -> str:
        """Produce a text diff against another node.

        Args:
            other: The node to diff against.
            rendered: Whether the diff should be between raw vs rendered nodes

        Returns:
            A unified text diff showing additions and deletions.
        """
        if not isinstance(other, StandaloneAudit):
            raise SQLMeshError(
                f"Cannot diff audit '{self.name} against a non-audit node '{other.name}'"
            )

        return d.text_diff(
            self.render_definition(render_query=rendered),
            other.render_definition(render_query=rendered),
            self.dialect,
            other.dialect,
        ).strip()

    def render_definition(
        self,
        include_python: bool = True,
        include_defaults: bool = False,
        render_query: bool = False,
    ) -> t.List[exp.Expr]:
        """Returns the original list of sql expressions comprising the model definition.

        Args:
            include_python: Whether or not to include Python code in the rendered definition.
        """
        expressions: t.List[exp.Expr] = []
        comment = None
        for field_name in sorted(self.meta_fields):
            field_value = getattr(self, field_name)
            field_info = self.all_field_infos()[field_name]
            if (
                field_name == "standalone"
                or (include_defaults and field_value)
                or field_value != field_info.default
            ):
                if field_name == "description":
                    comment = field_value
                else:
                    expression = exp.Property(
                        this=field_info.alias or field_name,
                        value=META_FIELD_CONVERTER.get(field_name, exp.to_identifier)(field_value),
                    )
                    if field_name == "name":
                        expressions.insert(0, expression)
                    else:
                        expressions.append(expression)

        audit = d.Audit(expressions=expressions)
        audit.comments = [comment] if comment else None

        jinja_expressions = []
        python_expressions = []
        if include_python:
            python_env = d.PythonCode(expressions=sorted_python_env_payloads(self.python_env))
            if python_env.expressions:
                python_expressions.append(python_env)

            jinja_expressions = self.jinja_macros.to_expressions()

        return [
            audit,
            *python_expressions,
            *jinja_expressions,
            *self.expressions,
            self.render_audit_query() if render_query else self.query,
        ]

    @property
    def is_audit(self) -> bool:
        """Return True if this is an audit node"""
        return True

    @property
    def meta_fields(self) -> t.Iterable[str]:
        return set(AuditCommonMetaMixin.__annotations__) | set(_Node.all_field_infos())

    @property
    def audits_with_args(self) -> t.List[t.Tuple[Audit, t.Dict[str, exp.Expr]]]:
        return [(self, {})]


Audit = t.Union[ModelAudit, StandaloneAudit]


def load_audit(
    expressions: t.List[exp.Expr],
    *,
    path: Path = Path(),
    module_path: Path = Path(),
    macros: t.Optional[MacroRegistry] = None,
    jinja_macros: t.Optional[JinjaMacroRegistry] = None,
    dialect: t.Optional[str] = None,
    default_catalog: t.Optional[str] = None,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    project: t.Optional[str] = None,
) -> Audit:
    """Load an audit from a parsed SQLMesh audit file.

    Args:
        expressions: Audit, *Statements, Query
        path: An optional path of the file.
        dialect: The default dialect if no audit dialect is configured.
    """
    if len(expressions) < 2:
        _raise_config_error("Incomplete audit definition, missing AUDIT or QUERY", path)

    meta, *statements, query = expressions

    if not isinstance(meta, d.Audit):
        _raise_config_error(
            "AUDIT statement is required as the first statement in the definition",
            path,
        )
        raise

    meta_fields = {p.name: p.args.get("value") for p in meta.expressions if p}

    standalone_field = meta_fields.pop("standalone", None)
    if standalone_field and not isinstance(standalone_field, exp.Boolean):
        _raise_config_error(
            f"""Standalone must be a boolean for '{meta_fields.get("name")}'""",
            path,
        )
        raise
    is_standalone = standalone_field and standalone_field.this

    audit_class: t.Union[t.Type[StandaloneAudit], t.Type[ModelAudit]] = (
        StandaloneAudit if is_standalone else ModelAudit
    )

    missing_required_fields = audit_class.missing_required_fields(set(meta_fields))
    missing_required_fields -= {"query"}
    if missing_required_fields:
        _raise_config_error(
            f"Missing required fields {missing_required_fields} in the audit definition",
            path,
        )

    extra_fields = audit_class.extra_fields(set(meta_fields))

    if extra_fields:
        _raise_config_error(f"Invalid extra fields {extra_fields} in the audit definition", path)

    if not isinstance(query, exp.Query) and not isinstance(query, d.JinjaQuery):
        _raise_config_error("Missing SELECT query in the audit definition", path)
        raise

    extra_kwargs: t.Dict[str, t.Any] = {}
    if is_standalone:
        jinja_macro_refrences, referenced_variables = extract_macro_references_and_variables(
            *(gen(s) for s in statements),
            gen(query),
        )
        jinja_macros = (jinja_macros or JinjaMacroRegistry()).trim(jinja_macro_refrences)
        for jinja_macro in jinja_macros.root_macros.values():
            referenced_variables.update(
                extract_macro_references_and_variables(jinja_macro.definition)[1]
            )

        extra_kwargs["jinja_macros"] = jinja_macros
        extra_kwargs["python_env"] = make_python_env(
            [*statements, query],
            jinja_macro_refrences,
            module_path,
            macros or macro.get_registry(),
            variables=variables,
            referenced_variables=referenced_variables,
        )
        extra_kwargs["default_catalog"] = default_catalog
        if project is not None:
            extra_kwargs["project"] = project

    dialect = meta_fields.pop("dialect", dialect) or ""

    parsable_query = ParsableSql.from_parsed_expression(query, dialect, use_meta_sql=True)
    parsable_statements = [
        ParsableSql.from_parsed_expression(s, dialect, use_meta_sql=True) for s in statements
    ]

    try:
        audit = audit_class(
            query=parsable_query,
            expressions=parsable_statements,
            dialect=dialect,
            **extra_kwargs,
            **meta_fields,
        )
    except Exception as ex:
        _raise_config_error(str(ex), path)

    audit._path = path
    return audit


def load_multiple_audits(
    expressions: t.List[exp.Expr],
    *,
    path: Path = Path(),
    module_path: Path = Path(),
    macros: t.Optional[MacroRegistry] = None,
    jinja_macros: t.Optional[JinjaMacroRegistry] = None,
    dialect: t.Optional[str] = None,
    default_catalog: t.Optional[str] = None,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    project: t.Optional[str] = None,
) -> t.Generator[Audit, None, None]:
    audit_block: t.List[exp.Expr] = []
    for expression in expressions:
        if isinstance(expression, d.Audit):
            if audit_block:
                yield load_audit(
                    expressions=audit_block,
                    path=path,
                    module_path=module_path,
                    macros=macros,
                    jinja_macros=jinja_macros,
                    dialect=dialect,
                    default_catalog=default_catalog,
                    variables=variables,
                    project=project,
                )
                audit_block.clear()
        audit_block.append(expression)
    yield load_audit(
        expressions=audit_block,
        path=path,
        dialect=dialect,
        default_catalog=default_catalog,
        variables=variables,
        project=project,
    )


def _raise_config_error(msg: str, path: pathlib.Path) -> None:
    raise_config_error(msg, location=path, error_type=AuditConfigError)


# mypy doesn't realize raise_config_error raises an exception
@t.no_type_check
def _maybe_parse_arg_pair(e: exp.Expr) -> t.Tuple[str, exp.Expr]:
    if isinstance(e, exp.EQ):
        return e.left.name, e.right


META_FIELD_CONVERTER: t.Dict[str, t.Callable] = {
    "start": lambda value: exp.Literal.string(value),
    "cron": lambda value: exp.Literal.string(value),
    "skip": exp.convert,
    "blocking": exp.convert,
    "standalone": exp.convert,
    "depends_on_": lambda value: exp.Tuple(expressions=sorted(value)),
    "tags": single_value_or_tuple,
    "default_catalog": exp.to_identifier,
    "dbt_node_info_": lambda value: value.to_expression(),
}
