from __future__ import annotations

import json
import logging
import types
import re
import typing as t
from functools import cached_property, partial
from pathlib import Path

from pydantic import Field
from sqlglot import diff, exp
from sqlglot.diff import Insert
from sqlglot.helper import seq_get
from sqlglot.optimizer.qualify_columns import quote_identifiers
from sqlglot.optimizer.simplify import gen
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.schema import MappingSchema, nested_set
from sqlglot.time import format_time

from sqlmesh.core import constants as c
from sqlmesh.core import dialect as d
from sqlmesh.core.audit import Audit, ModelAudit
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.macros import MacroRegistry, macro
from sqlmesh.core.model.common import (
    ParsableSql,
    make_python_env,
    parse_dependencies,
    parse_strings_with_macro_refs,
    single_value_or_tuple,
    sorted_python_env_payloads,
    validate_extra_and_required_fields,
)
from sqlmesh.core.model.meta import ModelMeta
from sqlmesh.core.model.kind import (
    ExternalKind,
    ModelKindName,
    SeedKind,
    ModelKind,
    FullKind,
    create_model_kind,
    CustomKind,
)
from sqlmesh.core.model.seed import CsvSeedReader, Seed, create_seed
from sqlmesh.core.renderer import ExpressionRenderer, QueryRenderer
from sqlmesh.core.signal import SignalRegistry
from sqlmesh.utils import columns_to_types_all_known, str_to_bool, UniqueKeyDict
from sqlmesh.utils.cron import CroniterCache
from sqlmesh.utils.date import TimeLike, make_inclusive, to_datetime, to_time_column
from sqlmesh.utils.errors import ConfigError, SQLMeshError, raise_config_error, PythonModelEvalError
from sqlmesh.utils.hashing import hash_data
from sqlmesh.utils.jinja import JinjaMacroRegistry, extract_macro_references_and_variables
from sqlmesh.utils.pydantic import PydanticModel, PRIVATE_FIELDS
from sqlmesh.utils.metaprogramming import (
    Executable,
    SqlValue,
    build_env,
    prepare_env,
    serialize_env,
    format_evaluated_code_exception,
)

if t.TYPE_CHECKING:
    from sqlglot.dialects.dialect import DialectType
    from sqlmesh.core.node import _Node
    from sqlmesh.core._typing import Self, TableName, SessionProperties
    from sqlmesh.core.context import ExecutionContext
    from sqlmesh.core.engine_adapter import EngineAdapter
    from sqlmesh.core.engine_adapter._typing import QueryOrDF
    from sqlmesh.core.engine_adapter.shared import DataObjectType
    from sqlmesh.core.linter.rule import Rule
    from sqlmesh.core.snapshot import DeployabilityIndex, Node, Snapshot
    from sqlmesh.utils.jinja import MacroReference


logger = logging.getLogger(__name__)


PROPERTIES = {"physical_properties", "session_properties", "virtual_properties"}

RUNTIME_RENDERED_MODEL_FIELDS = {
    "audits",
    "signals",
    "merge_filter",
} | PROPERTIES

CRON_SHORTCUTS = {
    "@midnight",
    "@hourly",
    "@daily",
    "@weekly",
    "@monthly",
    "@yearly",
    "@annually",
}


class _Model(ModelMeta, frozen=True):
    """Model is the core abstraction for user defined datasets.

    A model consists of logic that fetches the data (a SQL query, a Python script or a seed) and metadata
    associated with it. Models can be run on arbitrary cadences and support incremental or full refreshes.
    Models can also be materialized into physical tables or shared across other models as temporary views.

    Example:
        MODEL (
            name           sushi.order_items,
            owner          jen,
            cron           '@daily',
            start          '2020-01-01',
            partitioned_by ds
        );

        @DEF(var, 'my_var');

        SELECT
          1 AS column_a # my first column,
          @var AS my_column # my second column,
        ;

    Args:
        name: The name of the model, which is of the form [catalog].[db].table.
            The catalog and db are optional.
        dialect: The SQL dialect that the model's query is written in. By default,
            this is assumed to be the dialect of the context.
        owner: The owner of the model.
        cron: A cron string specifying how often the model should be refreshed, leveraging the
            [croniter](https://github.com/kiorky/croniter) library.
        description: The optional model description.
        stamp: An optional arbitrary string sequence used to create new model versions without making
            changes to any of the functional components of the definition.
        start: The earliest date that the model will be backfilled for. If this is None,
            then the date is inferred by taking the most recent start date of its ancestors.
            The start date can be a static datetime or a relative datetime like "1 year ago"
        end: The date that the model will be backfilled up until. Follows the same syntax as 'start',
            should be omitted if there is no end date.
        lookback: The number of previous incremental intervals in the lookback window.
        table_format: The table format used to manage the physical table files defined by `storage_format`, only applicable in certain engines.
            (eg, 'iceberg', 'delta', 'hudi')
        storage_format: The storage format used to store the physical table, only applicable in certain engines.
            (eg. 'parquet', 'orc')
        partitioned_by: The partition columns or engine specific expressions, only applicable in certain engines. (eg. (ds, hour))
        clustered_by: The cluster columns or engine specific expressions, only applicable in certain engines. (eg. (ds, hour))
        python_env: Dictionary containing all global variables needed to render the model's macros.
        mapping_schema: The schema of table names to column and types.
        extract_dependencies_from_query: Whether to extract additional dependencies from the rendered model's query.
        physical_schema_override: The desired physical schema name override.
    """

    python_env: t.Dict[str, Executable] = {}
    jinja_macros: JinjaMacroRegistry = JinjaMacroRegistry()
    audit_definitions: t.Dict[str, ModelAudit] = {}
    mapping_schema: t.Dict[str, t.Any] = {}
    extract_dependencies_from_query: bool = True
    pre_statements_: t.Optional[t.List[ParsableSql]] = Field(default=None, alias="pre_statements")
    post_statements_: t.Optional[t.List[ParsableSql]] = Field(default=None, alias="post_statements")
    on_virtual_update_: t.Optional[t.List[ParsableSql]] = Field(
        default=None, alias="on_virtual_update"
    )

    _full_depends_on: t.Optional[t.Set[str]] = None
    _statement_renderer_cache: t.Dict[int, ExpressionRenderer] = {}
    _is_metadata_only_change_cache: t.Dict[int, bool] = {}

    _expressions_validator = ParsableSql.validator()

    def __getstate__(self) -> t.Dict[t.Any, t.Any]:
        state = super().__getstate__()
        private = state[PRIVATE_FIELDS]
        private["_statement_renderer_cache"] = {}
        return state

    def copy(self, **kwargs: t.Any) -> Self:
        model = super().copy(**kwargs)
        model._statement_renderer_cache = {}
        return model

    def render(
        self,
        *,
        context: ExecutionContext,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        **kwargs: t.Any,
    ) -> t.Iterator[QueryOrDF]:
        """Renders the content of this model in a form of either a SELECT query, executing which the data for this model can
        be fetched, or a dataframe object which contains the data itself.

        The type of the returned object (query or dataframe) depends on whether the model was sourced from a SQL query,
        a Python script or a pre-built dataset (seed).

        Args:
            context: The execution context used for fetching data.
            start: The start date/time of the run.
            end: The end date/time of the run.
            execution_time: The date/time time reference to use for execution time.

        Returns:
            A generator which yields either a query object or one of the supported dataframe objects.
        """
        yield self.render_query_or_raise(
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=context.snapshots,
            deployability_index=context.deployability_index,
            engine_adapter=context.engine_adapter,
            **kwargs,
        )

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
        expressions = []
        comment = None
        for field_name, field_info in ModelMeta.all_field_infos().items():
            field_value = getattr(self, field_name)

            if (include_defaults and field_value) or field_value != field_info.default:
                if field_name == "description":
                    comment = field_value
                elif field_name == "kind":
                    expressions.append(
                        exp.Property(
                            this="kind",
                            value=field_value.to_expression(dialect=self.dialect),
                        )
                    )
                elif field_name == "name":
                    expressions.append(
                        exp.Property(
                            this=field_name,
                            value=exp.to_table(field_value, dialect=self.dialect),
                        )
                    )
                elif field_name not in ("default_catalog", "enabled", "ignored_rules_"):
                    expressions.append(
                        exp.Property(
                            this=field_info.alias or field_name,
                            value=META_FIELD_CONVERTER.get(field_name, exp.to_identifier)(
                                field_value
                            ),
                        )
                    )

        model = d.Model(expressions=expressions)
        model.comments = [comment] if comment else None

        jinja_expressions = []
        python_expressions = []
        if include_python:
            python_env = d.PythonCode(expressions=sorted_python_env_payloads(self.python_env))
            if python_env.expressions:
                python_expressions.append(python_env)

            jinja_expressions = self.jinja_macros.to_expressions()

        return [
            model,
            *python_expressions,
            *jinja_expressions,
        ]

    def render_query(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        table_mapping: t.Optional[t.Dict[str, str]] = None,
        expand: t.Iterable[str] = tuple(),
        deployability_index: t.Optional[DeployabilityIndex] = None,
        engine_adapter: t.Optional[EngineAdapter] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Query]:
        """Renders a model's query, expanding macros with provided kwargs, and optionally expanding referenced models.

        Args:
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time.
            snapshots: All upstream snapshots (by name) to use for expansion and mapping of physical locations.
            table_mapping: Table mapping of physical locations. Takes precedence over snapshot mappings.
            expand: Expand referenced models as subqueries. This is used to bypass backfills when running queries
                that depend on materialized tables.  Model definitions are inlined and can thus be run end to
                end on the fly.
            deployability_index: Determines snapshots that are deployable in the context of this render.
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            The rendered expression.
        """
        return exp.select(
            *(
                exp.cast(exp.Null(), column_type, copy=False).as_(name, copy=False, quoted=True)
                for name, column_type in (self.columns_to_types or {}).items()
            ),
            copy=False,
        ).from_(exp.values([tuple([1])], alias="t", columns=["dummy"]), copy=False)

    def render_query_or_raise(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        table_mapping: t.Optional[t.Dict[str, str]] = None,
        expand: t.Iterable[str] = tuple(),
        deployability_index: t.Optional[DeployabilityIndex] = None,
        engine_adapter: t.Optional[EngineAdapter] = None,
        **kwargs: t.Any,
    ) -> exp.Query:
        """Same as `render_query()` but raises an exception if the query can't be rendered.

        Args:
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time.
            snapshots: All upstream snapshots (by model name) to use for expansion and mapping of physical locations.
            table_mapping: Table mapping of physical locations. Takes precedence over snapshot mappings.
            expand: Expand referenced models as subqueries. This is used to bypass backfills when running queries
                that depend on materialized tables.  Model definitions are inlined and can thus be run end to
                end on the fly.
            deployability_index: Determines snapshots that are deployable in the context of this render.
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            The rendered expression.
        """
        query = self.render_query(
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            table_mapping=table_mapping,
            expand=expand,
            deployability_index=deployability_index,
            engine_adapter=engine_adapter,
            **kwargs,
        )
        if query is None:
            raise SQLMeshError(f"Failed to render query for model '{self.name}'.")
        return query

    def render_pre_statements(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Collection[Snapshot]] = None,
        expand: t.Iterable[str] = tuple(),
        deployability_index: t.Optional[DeployabilityIndex] = None,
        engine_adapter: t.Optional[EngineAdapter] = None,
        inside_transaction: t.Optional[bool] = True,
        **kwargs: t.Any,
    ) -> t.List[exp.Expr]:
        """Renders pre-statements for a model.

        Pre-statements are statements that preceded the model's SELECT query.

        Args:
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time.
            snapshots: All upstream snapshots (by model name) to use for expansion and mapping of physical locations.
            expand: Expand referenced models as subqueries. This is used to bypass backfills when running queries
                that depend on materialized tables.  Model definitions are inlined and can thus be run end to
                end on the fly.
            deployability_index: Determines snapshots that are deployable in the context of this render.
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            The list of rendered expressions.
        """
        return self._render_statements(
            [
                stmt
                for stmt in self.pre_statements
                if stmt.args.get("transaction", True) == inside_transaction
            ],
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            expand=expand,
            deployability_index=deployability_index,
            engine_adapter=engine_adapter,
            **kwargs,
        )

    def render_post_statements(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        expand: t.Iterable[str] = tuple(),
        deployability_index: t.Optional[DeployabilityIndex] = None,
        engine_adapter: t.Optional[EngineAdapter] = None,
        inside_transaction: t.Optional[bool] = True,
        **kwargs: t.Any,
    ) -> t.List[exp.Expr]:
        """Renders post-statements for a model.

        Post-statements are statements that follow after the model's SELECT query.

        Args:
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time.
            snapshots: All upstream snapshots (by model name) to use for expansion and mapping of physical locations.
            expand: Expand referenced models as subqueries. This is used to bypass backfills when running queries
                that depend on materialized tables.  Model definitions are inlined and can thus be run end to
                end on the fly.
            deployability_index: Determines snapshots that are deployable in the context of this render.
            inside_transaction: Whether to render hooks with transaction=True (inside) or transaction=False (outside).
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            The list of rendered expressions.
        """
        return self._render_statements(
            [
                stmt
                for stmt in self.post_statements
                if stmt.args.get("transaction", True) == inside_transaction
            ],
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            expand=expand,
            deployability_index=deployability_index,
            engine_adapter=engine_adapter,
            **kwargs,
        )

    def render_on_virtual_update(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        expand: t.Iterable[str] = tuple(),
        deployability_index: t.Optional[DeployabilityIndex] = None,
        engine_adapter: t.Optional[EngineAdapter] = None,
        **kwargs: t.Any,
    ) -> t.List[exp.Expr]:
        return self._render_statements(
            self.on_virtual_update,
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            expand=expand,
            deployability_index=deployability_index,
            engine_adapter=engine_adapter,
            **kwargs,
        )

    def render_audit_query(
        self,
        audit: Audit,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        **kwargs: t.Any,
    ) -> exp.Query:
        from sqlmesh.core.snapshot import DeployabilityIndex

        deployability_index = deployability_index or DeployabilityIndex.all_deployable()
        snapshot = (snapshots or {}).get(self.fqn)

        this_model = kwargs.pop("this_model", None) or (
            snapshot.table_name(deployability_index.is_deployable(snapshot))
            if snapshot
            else self.fqn
        )

        columns_to_types: t.Optional[t.Dict[str, t.Any]] = None
        if "engine_adapter" in kwargs:
            try:
                columns_to_types = kwargs["engine_adapter"].columns(this_model)
            except Exception:
                pass

        if self.time_column:
            low, high = [
                self.convert_to_time_column(dt, columns_to_types)
                for dt in make_inclusive(start or c.EPOCH, end or c.EPOCH, self.dialect)
            ]
            where = self.time_column.column.between(low, high)
        else:
            where = None

        # The model's name is already normalized, but in case of snapshots we also prepend a
        # case-sensitive physical schema name, so we quote here to ensure that we won't have
        # a broken schema reference after the resulting query is normalized in `render`.
        quoted_model_name = quote_identifiers(
            exp.to_table(this_model, dialect=self.dialect), dialect=self.dialect
        )

        query_renderer = QueryRenderer(
            audit.query,
            audit.dialect or self.dialect,
            audit.macro_definitions,
            path=audit._path or Path(),
            jinja_macro_registry=audit.jinja_macros,
            python_env=self.python_env,
            only_execution_time=self.kind.only_execution_time,
            default_catalog=self.default_catalog,
        )

        rendered_query = query_renderer.render(
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            deployability_index=deployability_index,
            **{
                **audit.defaults,
                "this_model": exp.select("*").from_(quoted_model_name).where(where).subquery()
                if where is not None
                else quoted_model_name,
                **kwargs,
            },  # type: ignore
        )

        if rendered_query is None:
            raise SQLMeshError(
                f"Failed to render query for audit '{audit.name}', model '{self.name}'."
            )

        return rendered_query

    @property
    def pre_statements(self) -> t.List[exp.Expr]:
        return self._get_parsed_statements("pre_statements_")

    @property
    def post_statements(self) -> t.List[exp.Expr]:
        return self._get_parsed_statements("post_statements_")

    @property
    def on_virtual_update(self) -> t.List[exp.Expr]:
        return self._get_parsed_statements("on_virtual_update_")

    @property
    def macro_definitions(self) -> t.List[d.MacroDef]:
        """All macro definitions from the list of expressions."""
        return [
            s
            for s in self.pre_statements + self.post_statements + self.on_virtual_update
            if isinstance(s, d.MacroDef)
        ]

    def _get_parsed_statements(self, attr_name: str) -> t.List[exp.Expr]:
        value = getattr(self, attr_name)
        if not value:
            return []
        result = []
        for v in value:
            parsed = v.parse(self.dialect)
            if getattr(v, "transaction", None) is not None:
                parsed.set("transaction", v.transaction)
            if not isinstance(parsed, exp.Semicolon):
                result.append(parsed)
        return result

    def _render_statements(
        self,
        statements: t.Iterable[exp.Expr],
        **kwargs: t.Any,
    ) -> t.List[exp.Expr]:
        rendered = (
            self._statement_renderer(statement).render(**kwargs)
            for statement in statements
            if not isinstance(statement, d.MacroDef)
        )
        return [r for expressions in rendered if expressions for r in expressions]

    def _statement_renderer(self, expression: exp.Expr) -> ExpressionRenderer:
        expression_key = id(expression)
        if expression_key not in self._statement_renderer_cache:
            self._statement_renderer_cache[expression_key] = ExpressionRenderer(
                expression,
                self.dialect,
                self.macro_definitions,
                path=self._path,
                jinja_macro_registry=self.jinja_macros,
                python_env=self.python_env,
                only_execution_time=False,
                default_catalog=self.default_catalog,
                model=self,
            )
        return self._statement_renderer_cache[expression_key]

    def render_signals(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
    ) -> t.List[t.Dict[str, str | int | float | bool]]:
        """Renders external; signals defined for this model.

        Args:
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time.

        Returns:
            The list of rendered expressions.
        """

        def _render(e: exp.Expr) -> str | int | float | bool:
            rendered_exprs = (
                self._create_renderer(e).render(start=start, end=end, execution_time=execution_time)
                or []
            )
            if len(rendered_exprs) != 1:
                raise SQLMeshError(f"Expected one expression but got {len(rendered_exprs)}")

            rendered = rendered_exprs[0]
            if rendered.is_int:
                return int(rendered.this)
            if rendered.is_number:
                return float(rendered.this)
            if isinstance(rendered, (exp.Literal, exp.Boolean)):
                return rendered.this
            return rendered.sql(dialect=self.dialect)

        # airflow only
        return [
            {k: _render(v) for k, v in signal.items()} for name, signal in self.signals if not name
        ]

    def render_signal_calls(self) -> EvaluatableSignals:
        python_env = self.python_env
        env = prepare_env(python_env)
        signals_to_kwargs = {
            name: {
                k: seq_get(self._create_renderer(v).render() or [], 0) for k, v in kwargs.items()
            }
            for name, kwargs in self.signals
            if name
        }

        return EvaluatableSignals(
            signals_to_kwargs=signals_to_kwargs,
            python_env=python_env,
            prepared_python_env=env,
        )

    def render_merge_filter(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
    ) -> t.Optional[exp.Expr]:
        if self.merge_filter is None:
            return None
        rendered_exprs = (
            self._create_renderer(self.merge_filter).render(
                start=start, end=end, execution_time=execution_time
            )
            or []
        )
        if len(rendered_exprs) != 1:
            raise SQLMeshError(f"Expected one expression but got {len(rendered_exprs)}")
        return rendered_exprs[0].transform(d.replace_merge_table_aliases, dialect=self.dialect)

    def _render_properties(
        self, properties: t.Dict[str, exp.Expr] | SessionProperties, **render_kwargs: t.Any
    ) -> t.Dict[str, t.Any]:
        def _render(expression: exp.Expr) -> exp.Expr | None:
            # note: we use the _statement_renderer instead of _create_renderer because it sets model_fqn which
            # in turn makes @this_model available in the evaluation context
            rendered_exprs = self._statement_renderer(expression).render(**render_kwargs)

            # Inform instead of raising for cases where a property is conditionally assigned
            if not rendered_exprs or rendered_exprs[0].sql().lower() in {"none", "null"}:
                logger.info(
                    f"Rendering '{expression.sql(dialect=self.dialect)}' did not return an expression"
                )
                return None

            if len(rendered_exprs) != 1:
                raise SQLMeshError(
                    f"Expected one result when rendering '{expression.sql(dialect=self.dialect)}' but got {len(rendered_exprs)}"
                )

            return rendered_exprs[0]

        return {
            k: rendered
            for k, v in properties.items()
            if (rendered := (_render(v) if isinstance(v, exp.Expr) else v))
        }

    def render_physical_properties(self, **render_kwargs: t.Any) -> t.Dict[str, t.Any]:
        rendered = self._render_properties(properties=self.physical_properties, **render_kwargs)

        # Some engines (e.g. StarRocks) accept properties whose values reference other models and
        # need the physical table name rather than the logical view SQLMesh exposes. Resolve those.
        engine_adapter = render_kwargs.get("engine_adapter")
        resolve_keys: t.FrozenSet[str] = getattr(
            engine_adapter, "RESOLVE_TABLE_REFS_IN_PHYSICAL_PROPERTIES", frozenset()
        )
        keys_to_resolve = [key for key in resolve_keys if key in rendered]
        if keys_to_resolve:
            # Local import: sqlmesh.core.snapshot.definition imports _Model, so importing
            # to_table_mapping at module scope would be circular.
            from sqlmesh.core.snapshot.definition import to_table_mapping

            table_mapping = to_table_mapping(
                (render_kwargs.get("snapshots") or {}).values(),
                render_kwargs.get("deployability_index"),
            )
            for key in keys_to_resolve:
                rendered[key] = _resolve_model_refs_to_physical_tables(
                    rendered[key], table_mapping, self.dialect
                )

        return rendered

    def render_virtual_properties(self, **render_kwargs: t.Any) -> t.Dict[str, t.Any]:
        return self._render_properties(properties=self.virtual_properties, **render_kwargs)

    def render_session_properties(self, **render_kwargs: t.Any) -> t.Dict[str, t.Any]:
        return self._render_properties(properties=self.session_properties, **render_kwargs)

    def _create_renderer(self, expression: exp.Expr) -> ExpressionRenderer:
        return ExpressionRenderer(
            expression,
            self.dialect,
            [],
            path=self._path,
            jinja_macro_registry=self.jinja_macros,
            python_env=self.python_env,
            only_execution_time=False,
            quote_identifiers=False,
        )

    def ctas_query(self, **render_kwarg: t.Any) -> exp.Query:
        """Return a dummy query to do a CTAS.

        If a model's column types are unknown, the only way to create the table is to
        run the fully expanded query. This can be expensive so we add a WHERE FALSE to all
        SELECTS and hopefully the optimizer is smart enough to not do anything.

        Args:
            render_kwarg: Additional kwargs to pass to the renderer.
        Return:
            The mocked out ctas query.
        """
        query = self.render_query_or_raise(**render_kwarg).limit(0)

        for select_or_set_op in query.find_all(exp.Select, exp.SetOperation):
            if isinstance(select_or_set_op, exp.Select) and select_or_set_op.args.get("from_"):
                select_or_set_op.where(exp.false(), copy=False)

        if self.managed_columns:
            query.select(
                *[
                    exp.alias_(exp.cast(exp.Null(), to=col_type), col)
                    for col, col_type in self.managed_columns.items()
                    if col not in query.named_selects
                ],
                append=True,
                copy=False,
            )
        return query

    def text_diff(self, other: Node, rendered: bool = False) -> str:
        """Produce a text diff against another node.

        Args:
            other: The node to diff against.
            rendered: Whether the diff should compare raw vs rendered models

        Returns:
            A unified text diff showing additions and deletions.
        """
        if not isinstance(other, _Model):
            raise SQLMeshError(
                f"Cannot diff model '{self.name} against a non-model node '{other.name}'"
            )

        text_diff = d.text_diff(
            self.render_definition(render_query=rendered),
            other.render_definition(render_query=rendered),
            self.dialect,
            other.dialect,
        ).strip()

        if not text_diff and not rendered:
            text_diff = d.text_diff(
                self.render_definition(render_query=True),
                other.render_definition(render_query=True),
                self.dialect,
                other.dialect,
            ).strip()

        return text_diff

    def set_time_format(self, default_time_format: str = c.DEFAULT_TIME_COLUMN_FORMAT) -> None:
        """Sets the default time format for a model.

        Args:
            default_time_format: A python time format used as the default format when none is provided.
        """
        if not self.time_column:
            return

        if self.time_column.format:
            # Transpile the time column format into the generic dialect
            formatted_time = format_time(
                self.time_column.format,
                d.Dialect.get_or_raise(self.dialect).TIME_MAPPING,
            )
            assert formatted_time is not None
            self.time_column.format = formatted_time
        else:
            self.time_column.format = default_time_format

    def convert_to_time_column(
        self, time: TimeLike, columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None
    ) -> exp.Expr:
        """Convert a TimeLike object to the same time format and type as the model's time column."""
        if self.time_column:
            if columns_to_types is None:
                columns_to_types = self.columns_to_types_or_raise

            if self.time_column.column.name not in columns_to_types:
                raise ConfigError(
                    f"Time column '{self.time_column.column.sql(dialect=self.dialect)}' not found in model '{self.name}'."
                )

            time_column_type = columns_to_types[self.time_column.column.name]

            return to_time_column(
                time,
                time_column_type,
                self.dialect,
                self.time_column.format,
            )
        return exp.convert(time)

    def set_mapping_schema(self, schema: t.Dict) -> None:
        self.mapping_schema.clear()
        self.mapping_schema.update(schema)

    def update_schema(self, schema: MappingSchema) -> None:
        """Updates the schema for this model's dependencies based on the given mapping schema."""
        for dep in self.depends_on:
            table = exp.to_table(dep)
            mapping_schema = schema.find(table)

            if mapping_schema:
                nested_set(
                    self.mapping_schema,
                    tuple(part.sql(copy=False) for part in table.parts),
                    {col: dtype.sql(dialect=self.dialect) for col, dtype in mapping_schema.items()},
                )

    @property
    def depends_on(self) -> t.Set[str]:
        """All of the upstream dependencies referenced in the model's query, excluding self references.

        Returns:
            A list of all the upstream table names.
        """
        return self.full_depends_on - {self.fqn}

    @property
    def columns_to_types(self) -> t.Optional[t.Dict[str, exp.DataType]]:
        """Returns the mapping of column names to types of this model."""
        if self.columns_to_types_ is None:
            return None
        return {**self.columns_to_types_, **self.managed_columns}

    @property
    def columns_to_types_or_raise(self) -> t.Dict[str, exp.DataType]:
        """Returns the mapping of column names to types of this model or raise if not available."""
        columns_to_types = self.columns_to_types
        if columns_to_types is None:
            raise SQLMeshError(f"Column information is not available for model '{self.name}'")
        return columns_to_types

    @property
    def annotated(self) -> bool:
        """Checks if all column projection types of this model are known."""
        if self.columns_to_types is None:
            return False
        columns_to_types = {
            k: v for k, v in self.columns_to_types.items() if k not in self.managed_columns
        }
        if not columns_to_types:
            return False
        return columns_to_types_all_known(columns_to_types)

    @property
    def sorted_python_env(self) -> t.List[t.Tuple[str, Executable]]:
        """Returns the python env sorted by executable kind and then var name."""
        return sorted(self.python_env.items(), key=lambda x: (x[1].kind, x[0]))

    @property
    def view_name(self) -> str:
        return self.fully_qualified_table.name

    @property
    def schema_name(self) -> str:
        return self.fully_qualified_table.db or c.DEFAULT_SCHEMA

    @property
    def physical_schema(self) -> str:
        return self.physical_schema_override or f"{c.SQLMESH}__{self.schema_name}"

    @property
    def is_sql(self) -> bool:
        return False

    @property
    def is_python(self) -> bool:
        return False

    @property
    def is_seed(self) -> bool:
        return False

    @property
    def depends_on_self(self) -> bool:
        return self.fqn in self.full_depends_on

    @property
    def forward_only(self) -> bool:
        return getattr(self.kind, "forward_only", False)

    @property
    def disable_restatement(self) -> bool:
        return getattr(self.kind, "disable_restatement", False)

    @property
    def auto_restatement_intervals(self) -> t.Optional[int]:
        return getattr(self.kind, "auto_restatement_intervals", None)

    @property
    def auto_restatement_cron(self) -> t.Optional[str]:
        return getattr(self.kind, "auto_restatement_cron", None)

    def auto_restatement_croniter(self, value: TimeLike) -> CroniterCache:
        cron = self.auto_restatement_cron
        if cron is None:
            raise SQLMeshError("Auto restatement cron is not set.")
        return CroniterCache(cron, value)

    @property
    def wap_supported(self) -> bool:
        return self.kind.is_materialized and (self.storage_format or "").lower() == "iceberg"

    def validate_definition(self) -> None:
        """Validates the model's definition.

        Raises:
            ConfigError
        """

        for field in ("partitioned_by", "clustered_by"):
            values = getattr(self, field)

            if values:
                values = [
                    col.name
                    for expr in values
                    for col in t.cast(
                        exp.Expr, exp.maybe_parse(expr, dialect=self.dialect)
                    ).find_all(exp.Column)
                ]

                unique_keys = set(values)

                if len(values) != len(unique_keys):
                    raise_config_error(
                        f"All keys in '{field}' must be unique in the model definition",
                        self._path,
                    )

                columns_to_types = self.columns_to_types
                if columns_to_types is not None:
                    missing_keys = unique_keys - set(columns_to_types)
                    if missing_keys:
                        missing_keys_str = ", ".join(f"'{k}'" for k in sorted(missing_keys))
                        raise_config_error(
                            f"{field} keys [{missing_keys_str}] are missing in the model definition",
                            self._path,
                        )

        if self.kind.is_incremental_by_time_range and not self.time_column:
            raise_config_error(
                "Incremental by time range models must have a time_column field",
                self._path,
            )

        if (
            self.kind.is_incremental_unmanaged
            and getattr(self.kind, "insert_overwrite", False)
            and not self.partitioned_by_
        ):
            raise_config_error(
                "Unmanaged incremental models with insert / overwrite enabled must specify the partitioned_by field",
                self._path,
            )

        if self.kind.is_managed:
            # TODO: would this sort of logic be better off moved into the Kind?
            if self.dialect == "snowflake" and "target_lag" not in self.physical_properties:
                raise_config_error(
                    "Snowflake managed tables must specify the 'target_lag' physical property",
                    self._path,
                )

        if self.physical_version is not None and not self.forward_only:
            raise_config_error(
                "Pinning a physical version is only supported for forward only models",
                self._path,
            )

        # The following attributes should be set only for SQL models
        if not self.is_sql:
            if self.optimize_query:
                raise_config_error(
                    "SQLMesh query optimizer can only be enabled for SQL models",
                    self._path,
                )

        if isinstance(self.kind, CustomKind):
            from sqlmesh.core.snapshot.evaluator import get_custom_materialization_type_or_raise

            # Will raise if the custom materialization points to an invalid class
            get_custom_materialization_type_or_raise(self.kind.materialization)

        # Embedded model kind shouldn't have audits
        if self.kind.name == ModelKindName.EMBEDDED and self.audits:
            raise_config_error(
                "Audits are not supported for embedded models",
                self._path,
            )

    def is_breaking_change(self, previous: Model) -> t.Optional[bool]:
        """Determines whether this model is a breaking change in relation to the `previous` model.

        Args:
            previous: The previous model to compare against.

        Returns:
            True if this model instance represents a breaking change, False if it's a non-breaking change
            and None if the nature of the change can't be determined.
        """
        raise NotImplementedError

    def is_metadata_only_change(self, other: _Node) -> bool:
        if self._is_metadata_only_change_cache.get(id(other), None) is not None:
            return self._is_metadata_only_change_cache[id(other)]

        is_metadata_change = True
        if (
            not isinstance(other, _Model)
            or self.metadata_hash == other.metadata_hash
            or self._data_hash_values_no_sql != other._data_hash_values_no_sql
        ):
            is_metadata_change = False
        else:
            this_statements = [
                s
                for s in [*self.pre_statements, *self.post_statements]
                if not self._is_metadata_statement(s)
            ]
            other_statements = [
                s
                for s in [*other.pre_statements, *other.post_statements]
                if not other._is_metadata_statement(s)
            ]
            if len(this_statements) != len(other_statements):
                is_metadata_change = False
            else:
                for this_statement, other_statement in zip(this_statements, other_statements):
                    this_rendered = (
                        self._statement_renderer(this_statement).render() or this_statement
                    )
                    other_rendered = (
                        other._statement_renderer(other_statement).render() or other_statement
                    )
                    if this_rendered != other_rendered:
                        is_metadata_change = False
                        break

        self._is_metadata_only_change_cache[id(other)] = is_metadata_change
        return is_metadata_change

    @property
    def data_hash(self) -> str:
        """
        Computes the data hash for the node.

        Returns:
            The data hash for the node.
        """
        if self._data_hash is None:
            self._data_hash = hash_data(self._data_hash_values)
        return self._data_hash

    @property
    def _data_hash_values(self) -> t.List[str]:
        return self._data_hash_values_no_sql + self._data_hash_values_sql

    @property
    def _data_hash_values_sql(self) -> t.List[str]:
        data = []

        for statements in [self.pre_statements_, self.post_statements_]:
            for statement in statements or []:
                data.append(statement.sql)

        return data

    @property
    def _data_hash_values_no_sql(self) -> t.List[str]:
        data = [
            str(  # Exclude metadata only macro funcs
                [(k, v) for k, v in self.sorted_python_env if not v.is_metadata]
            ),
            *self.kind.data_hash_values,
            self.table_format,
            self.storage_format,
            str(self.lookback),
            *(gen(expr) for expr in (self.partitioned_by or [])),
            *(gen(expr) for expr in (self.clustered_by or [])),
            self.stamp,
            self.physical_schema,
            self.physical_version,
            self.gateway,
            self.interval_unit.value if self.interval_unit is not None else None,
            str(self.optimize_query) if self.optimize_query is not None else None,
            self.virtual_environment_mode.value,
        ]

        for column_name, column_type in (self.columns_to_types_ or {}).items():
            data.append(column_name)
            data.append(column_type.sql(dialect=self.dialect))

        for key, value in (self.physical_properties or {}).items():
            data.append(key)
            data.append(gen(value))

        return data  # type: ignore

    def _audit_metadata_hash_values(self) -> t.List[str]:
        from sqlmesh.core.audit.builtin import BUILT_IN_AUDITS

        metadata = []

        for audit_name, audit_args in sorted(self.audits, key=lambda a: a[0]):
            metadata.append(audit_name)
            if audit_name not in BUILT_IN_AUDITS:
                audit = self.audit_definitions[audit_name]
                metadata.extend(
                    [
                        audit.query_.sql,
                        audit.dialect,
                        str(audit.skip),
                        str(audit.blocking),
                    ]
                )
            for arg_name, arg_value in audit_args.items():
                metadata.append(arg_name)
                metadata.append(gen(arg_value))

        return metadata

    def audit_metadata_hash(self) -> str:
        return hash_data(self._audit_metadata_hash_values())

    @property
    def metadata_hash(self) -> str:
        """
        Computes the metadata hash for the node.

        Returns:
            The metadata hash for the node.
        """
        if self._metadata_hash is None:
            metadata = [
                self.dialect,
                self.owner,
                self.description,
                json.dumps(self.column_descriptions, sort_keys=True),
                self.cron,
                self.cron_tz.key if self.cron_tz else None,
                str(self.start) if self.start else None,
                str(self.end) if self.end else None,
                str(self.retention) if self.retention else None,
                str(self.batch_size) if self.batch_size is not None else None,
                str(self.batch_concurrency) if self.batch_concurrency is not None else None,
                json.dumps(self.mapping_schema, sort_keys=True),
                *sorted(self.tags),
                *sorted(ref.json(sort_keys=True) for ref in self.all_references),
                *self.kind.metadata_hash_values,
                self.project,
                str(self.allow_partials),
                gen(self.session_properties_) if self.session_properties_ else None,
                *[gen(g) for g in self.grains],
                *self._audit_metadata_hash_values(),
                json.dumps(self.grants, sort_keys=True) if self.grants else None,
                self.grants_target_layer,
            ]

            for key, value in (self.virtual_properties or {}).items():
                metadata.append(key)
                metadata.append(gen(value))

            for signal_name, args in sorted(self.signals, key=lambda x: x[0]):
                metadata.append(signal_name)
                for k, v in sorted(args.items()):
                    metadata.append(f"{k}:{gen(v)}")

            if self.dbt_node_info:
                metadata.append(self.dbt_node_info.json(sort_keys=True))

            metadata.extend(self._additional_metadata)

            self._metadata_hash = hash_data(metadata)
        return self._metadata_hash

    @property
    def is_model(self) -> bool:
        """Return True if this is a model node"""
        return True

    @property
    def grants_table_type(self) -> DataObjectType:
        """Get the table type for grants application (TABLE, VIEW, MATERIALIZED_VIEW).

        Returns:
            The DataObjectType that should be used when applying grants to this model.
        """
        from sqlmesh.core.engine_adapter.shared import DataObjectType

        if self.kind.is_view:
            if hasattr(self.kind, "materialized") and getattr(self.kind, "materialized", False):
                return DataObjectType.MATERIALIZED_VIEW
            return DataObjectType.VIEW
        if self.kind.is_managed:
            return DataObjectType.MANAGED_TABLE
        # All other materialized models are tables
        return DataObjectType.TABLE

    @property
    def _additional_metadata(self) -> t.List[str]:
        additional_metadata = []

        metadata_only_macros = [(k, v) for k, v in self.sorted_python_env if v.is_metadata]
        if metadata_only_macros:
            additional_metadata.append(str(metadata_only_macros))

        for statements in [self.pre_statements_, self.post_statements_, self.on_virtual_update_]:
            for statement in statements or []:
                additional_metadata.append(statement.sql)

        return additional_metadata

    def _is_metadata_statement(self, statement: exp.Expr) -> bool:
        if isinstance(statement, d.MacroDef):
            return True
        if isinstance(statement, d.MacroFunc):
            target_macro = macro.get_registry().get(statement.name)
            if target_macro:
                return target_macro.metadata_only
            target_macro = self.python_env.get(statement.name)
            if target_macro:
                return bool(target_macro.is_metadata)
        return False

    @property
    def full_depends_on(self) -> t.Set[str]:
        if not self.extract_dependencies_from_query:
            return self.depends_on_ or set()
        if self._full_depends_on is None:
            depends_on = self.depends_on_ or set()

            query = self.render_query(needs_optimization=False)
            if query is not None:
                depends_on |= d.find_tables(
                    query, default_catalog=self.default_catalog, dialect=self.dialect
                )
            self._full_depends_on = depends_on

        return self._full_depends_on

    @property
    def partitioned_by(self) -> t.List[exp.Expr]:
        """Columns to partition the model by, including the time column if it is not already included."""
        if self.time_column and not self._is_time_column_in_partitioned_by:
            # This allows the user to opt out of automatic time_column injection
            # by setting `partition_by_time_column false` on the model kind
            if (
                hasattr(self.kind, "partition_by_time_column")
                and self.kind.partition_by_time_column
            ):
                return [
                    TIME_COL_PARTITION_FUNC.get(self.dialect, lambda x, y: x)(
                        self.time_column.column, self.columns_to_types
                    ),
                    *self.partitioned_by_,
                ]
        return self.partitioned_by_

    @property
    def partition_interval_unit(self) -> t.Optional[IntervalUnit]:
        """The interval unit to use for partitioning if applicable."""
        # Only return the interval unit for partitioning if the partitioning
        # wasn't explicitly set by the user. Otherwise, the user-provided
        # value should always take precedence.
        if self.time_column and not self._is_time_column_in_partitioned_by:
            return self.interval_unit
        return None

    @property
    def audits_with_args(self) -> t.List[t.Tuple[Audit, t.Dict[str, exp.Expr]]]:
        from sqlmesh.core.audit.builtin import BUILT_IN_AUDITS

        audits_by_name = {**BUILT_IN_AUDITS, **self.audit_definitions}
        audits_with_args = []
        added_audits = set()

        for audit_name, audit_args in self.audits:
            audits_with_args.append((audits_by_name[audit_name], audit_args.copy()))
            added_audits.add(audit_name)

        for audit_name in self.audit_definitions:
            if audit_name not in added_audits:
                audits_with_args.append((audits_by_name[audit_name], {}))

        return audits_with_args

    @property
    def _is_time_column_in_partitioned_by(self) -> bool:
        return self.time_column is not None and self.time_column.column in {
            col for expr in self.partitioned_by_ for col in expr.find_all(exp.Column)
        }

    @property
    def violated_rules_for_query(self) -> t.Dict[type[Rule], t.Any]:
        return {}


class SqlModel(_Model):
    """The model definition which relies on a SQL query to fetch the data.

    Args:
        query: The main query representing the model.
        pre_statements: The list of SQL statements that precede the model's query.
        post_statements: The list of SQL statements that follow after the model's query.
        on_virtual_update: The list of SQL statements to be executed after the virtual update.
    """

    query_: ParsableSql = Field(alias="query")
    source_type: t.Literal["sql"] = "sql"

    _columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None

    def __getstate__(self) -> t.Dict[t.Any, t.Any]:
        state = super().__getstate__()
        state["__dict__"] = state["__dict__"].copy()
        # query renderer is very expensive to serialize
        state["__dict__"].pop("_query_renderer", None)
        state["__dict__"].pop("column_descriptions", None)
        private = state[PRIVATE_FIELDS]
        private["_columns_to_types"] = None
        return state

    def copy(self, **kwargs: t.Any) -> Self:
        model = super().copy(**kwargs)
        model.__dict__.pop("_query_renderer", None)
        model.__dict__.pop("column_descriptions", None)
        model._columns_to_types = None
        if kwargs.get("update", {}).keys() & {"depends_on_", "query"}:
            model._full_depends_on = None
        return model

    @property
    def query(self) -> t.Union[exp.Query, d.JinjaQuery, d.MacroFunc]:
        parsed_query = self.query_.parse(self.dialect)
        return t.cast(t.Union[exp.Query, d.JinjaQuery, d.MacroFunc], parsed_query)

    def render_query(
        self,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        table_mapping: t.Optional[t.Dict[str, str]] = None,
        expand: t.Iterable[str] = tuple(),
        deployability_index: t.Optional[DeployabilityIndex] = None,
        engine_adapter: t.Optional[EngineAdapter] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Query]:
        query = self._query_renderer.render(
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            table_mapping=table_mapping,
            expand=expand,
            deployability_index=deployability_index,
            engine_adapter=engine_adapter,
            **kwargs,
        )

        return query

    def render_definition(
        self,
        include_python: bool = True,
        include_defaults: bool = False,
        render_query: bool = False,
    ) -> t.List[exp.Expr]:
        result: t.List[exp.Expr] = super().render_definition(
            include_python=include_python, include_defaults=include_defaults
        )

        if render_query:
            result.extend(self.render_pre_statements())
            result.append(self.render_query() or self.query)
            result.extend(self.render_post_statements())
            if virtual_update := self.render_on_virtual_update():
                result.append(d.VirtualUpdateStatement(expressions=virtual_update))
        else:
            result.extend(self.pre_statements)
            result.append(self.query)
            result.extend(self.post_statements)
            if self.on_virtual_update:
                result.append(d.VirtualUpdateStatement(expressions=self.on_virtual_update))

        return result

    @property
    def is_sql(self) -> bool:
        return True

    @property
    def columns_to_types(self) -> t.Optional[t.Dict[str, exp.DataType]]:
        if self.columns_to_types_ is not None:
            self._columns_to_types = self.columns_to_types_
        elif self._columns_to_types is None:
            try:
                query = self._query_renderer.render()
            except Exception:
                logger.exception("Failed to render query for model %s", self.fqn)
                return None

            if query is None:
                return None

            unknown = exp.DataType.build("unknown")

            columns_to_types = {}
            for select in query.selects:
                output_name = select.output_name

                # If model validation is disabled, we cannot assume that projections
                # will have inferrable output names or even that they will be unique
                if not output_name or output_name in columns_to_types:
                    return None

                # copy data type because it is used in the engine to build CTAS and other queries
                # this can change the parent which will mess up the diffing algo
                columns_to_types[output_name] = (select.type or unknown).copy()

            self._columns_to_types = columns_to_types

        if "*" in self._columns_to_types:
            return None

        return {**self._columns_to_types, **self.managed_columns}

    @cached_property
    def column_descriptions(self) -> t.Dict[str, str]:
        if self.column_descriptions_ is not None:
            return self.column_descriptions_

        query = self.render_query()
        if query is None:
            return {}

        return {
            select.alias_or_name: select.comments[-1].strip()
            for select in query.selects
            if select.comments
        }

    def set_mapping_schema(self, schema: t.Dict) -> None:
        super().set_mapping_schema(schema)
        self._on_mapping_schema_set()

    def update_schema(self, schema: MappingSchema) -> None:
        super().update_schema(schema)
        self._on_mapping_schema_set()

    def _on_mapping_schema_set(self) -> None:
        self._columns_to_types = None
        self._query_renderer.update_schema(self.mapping_schema)

    def validate_definition(self) -> None:
        query = self._query_renderer.render()
        if query is None:
            if self.depends_on_ is None:
                raise_config_error(
                    "Dependencies must be provided explicitly for models that can be rendered only at runtime",
                    self._path,
                )
            return

        if not isinstance(query, exp.Query):
            raise_config_error("Missing SELECT query in the model definition", self._path)

        projection_list = query.selects
        if not projection_list:
            raise_config_error("Query missing select statements", self._path)

        if self.depends_on_self and not self.annotated:
            raise_config_error(
                "Self-referencing models require inferrable column types. There are three options available to mitigate this issue: add explicit types to all projections in the outermost SELECT statement, leverage external models (https://sqlmesh.readthedocs.io/en/stable/concepts/models/external_models/), or use the `columns` model attribute (https://sqlmesh.readthedocs.io/en/stable/concepts/models/overview/#columns).",
                self._path,
            )

        super().validate_definition()

    def is_breaking_change(self, previous: Model) -> t.Optional[bool]:
        if not isinstance(previous, SqlModel):
            return None

        if self.lookback != previous.lookback:
            return None

        try:
            # the previous model which comes from disk could be unrenderable
            previous_query = previous.render_query()
        except Exception:
            previous_query = None
        this_query = self.render_query()

        if previous_query is None or this_query is None:
            # Can't determine if there's a breaking change if we can't render the query.
            return None

        if previous_query is this_query:
            edits = []
        else:
            edits = diff(
                previous_query,
                this_query,
                matchings=[(previous_query, this_query)],
                delta_only=True,
                dialect=self.dialect if self.dialect == previous.dialect else None,
            )
        inserted_expressions = {e.expression for e in edits if isinstance(e, Insert)}

        for edit in edits:
            if not isinstance(edit, Insert):
                return None

            expr = edit.expression
            if isinstance(expr, exp.UDTF):
                # projection subqueries do not change cardinality, engines don't allow these to return
                # more than one row of data
                parent = expr.find_ancestor(exp.Subquery)

                if not parent:
                    return None

                expr = parent

            if not _is_projection(expr) and expr.parent not in inserted_expressions:
                return None

        return False

    def is_metadata_only_change(self, previous: _Node) -> bool:
        if self._is_metadata_only_change_cache.get(id(previous), None) is not None:
            return self._is_metadata_only_change_cache[id(previous)]

        if not super().is_metadata_only_change(previous):
            return False

        if not isinstance(previous, SqlModel):
            self._is_metadata_only_change_cache[id(previous)] = False
            return False

        this_rendered_query = self.render_query() or self.query
        previous_rendered_query = previous.render_query() or previous.query
        is_metadata_change = this_rendered_query == previous_rendered_query

        self._is_metadata_only_change_cache[id(previous)] = is_metadata_change
        return is_metadata_change

    @cached_property
    def _query_renderer(self) -> QueryRenderer:
        no_quote_identifiers = self.kind.is_view and self.dialect in ("trino", "spark")
        return QueryRenderer(
            self.query,
            self.dialect,
            self.macro_definitions,
            schema=self.mapping_schema,
            path=self._path,
            jinja_macro_registry=self.jinja_macros,
            python_env=self.python_env,
            only_execution_time=self.kind.only_execution_time,
            default_catalog=self.default_catalog,
            quote_identifiers=not no_quote_identifiers,
            optimize_query=self.optimize_query,
            model=self,
        )

    @property
    def _data_hash_values_no_sql(self) -> t.List[str]:
        return [
            *super()._data_hash_values_no_sql,
            *self.jinja_macros.data_hash_values,
        ]

    @property
    def _data_hash_values_sql(self) -> t.List[str]:
        return [
            *super()._data_hash_values_sql,
            self.query_.sql,
        ]

    @property
    def _additional_metadata(self) -> t.List[str]:
        return [*super()._additional_metadata, self.query_.sql]

    @property
    def violated_rules_for_query(self) -> t.Dict[type[Rule], t.Any]:
        self.render_query()
        return self._query_renderer._violated_rules


class SeedModel(_Model):
    """The model definition which uses a pre-built static dataset to source the data from.

    Args:
        seed: The content of a pre-built static dataset.
    """

    kind: SeedKind
    seed: Seed
    column_hashes_: t.Optional[t.Dict[str, str]] = Field(default=None, alias="column_hashes")
    derived_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None
    is_hydrated: bool = True
    source_type: t.Literal["seed"] = "seed"

    def __getstate__(self) -> t.Dict[t.Any, t.Any]:
        state = super().__getstate__()
        state["__dict__"] = state["__dict__"].copy()
        state["__dict__"].pop("_reader", None)
        return state

    def copy(self, **kwargs: t.Any) -> Self:
        model = super().copy(**kwargs)
        model.__dict__.pop("_reader", None)
        return model

    def render(
        self,
        *,
        context: ExecutionContext,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        **kwargs: t.Any,
    ) -> t.Iterator[QueryOrDF]:
        if not self.is_hydrated:
            return
        yield from self.render_seed()

    def render_seed(self) -> t.Iterator[QueryOrDF]:
        import numpy as np

        self._ensure_hydrated()

        date_columns = []
        datetime_columns = []
        bool_columns = []
        string_columns = []

        columns_to_types = self.columns_to_types_ or {}
        column_names_to_check = set(columns_to_types)
        for name, tpe in columns_to_types.items():
            if tpe.this in (exp.DataType.Type.DATE, exp.DataType.Type.DATE32):
                date_columns.append(name)
            elif tpe.this in exp.DataType.TEMPORAL_TYPES:
                datetime_columns.append(name)
            elif tpe.is_type("boolean"):
                bool_columns.append(name)
            elif tpe.this in exp.DataType.TEXT_TYPES:
                string_columns.append(name)

        for df in self._reader.read(batch_size=self.kind.batch_size):
            rename_dict = {}
            for column in columns_to_types:
                if column not in df:
                    normalized_name = normalize_identifiers(column, dialect=self.dialect).name
                    if normalized_name in df:
                        rename_dict[normalized_name] = column
            if rename_dict:
                df.rename(columns=rename_dict, inplace=True)
                # These names have already been checked
                column_names_to_check -= set(rename_dict)

            missing_columns = column_names_to_check - set(df.columns)
            if missing_columns:
                raise_config_error(
                    f"Seed model '{self.name}' has missing columns: {missing_columns}", self._path
                )

            # convert all date/time types to native pandas timestamp
            for column in [*date_columns, *datetime_columns]:
                import pandas as pd

                df[column] = pd.to_datetime(df[column], infer_datetime_format=True, errors="ignore")  # type: ignore

            # extract datetime.date from pandas timestamp for DATE columns
            for column in date_columns:
                try:
                    df[column] = df[column].dt.date
                except Exception as ex:
                    logger.error(
                        "Failed to convert column '%s' to date in seed model '%s': %s",
                        column,
                        self.name,
                        ex,
                    )

            for column in bool_columns:
                df[column] = df[column].apply(lambda i: str_to_bool(str(i)))

            df.loc[:, string_columns] = df[string_columns].mask(
                cond=lambda x: x.notna(),  # type: ignore
                other=df[string_columns].astype(str),  # type: ignore
            )
            yield df.replace({np.nan: None})

    @property
    def columns_to_types(self) -> t.Optional[t.Dict[str, exp.DataType]]:
        if self.columns_to_types_ is not None:
            return self.columns_to_types_
        if self.derived_columns_to_types is not None:
            return self.derived_columns_to_types
        if self.is_hydrated:
            return self._reader.columns_to_types
        return None

    @property
    def column_hashes(self) -> t.Dict[str, str]:
        if self.column_hashes_ is not None:
            return self.column_hashes_
        self._ensure_hydrated()
        return self._reader.column_hashes

    @property
    def is_seed(self) -> bool:
        return True

    @property
    def seed_path(self) -> Path:
        seed_path = Path(self.kind.path)
        if not seed_path.is_absolute():
            if self._path is None:
                raise SQLMeshError(f"Seed model '{self.name}' has no path")
            return self._path.parent / seed_path
        return seed_path

    @property
    def depends_on(self) -> t.Set[str]:
        return (self.depends_on_ or set()) - {self.fqn}

    @property
    def depends_on_self(self) -> bool:
        return False

    @property
    def batch_size(self) -> t.Optional[int]:
        # Unlike other model kinds, the batch size provided in the SEED kind represents the
        # maximum number of rows to insert in a single batch.
        # We should never batch intervals for seed models.
        return None

    def to_dehydrated(self) -> SeedModel:
        """Creates a dehydrated copy of this model.

        The dehydrated seed model will not contain the seed content, but will contain
        the column hashes. This is useful for comparing two seed models without
        having to read the seed content from disk.

        Returns:
            A dehydrated copy of this model.
        """
        if not self.is_hydrated:
            return self

        return self.copy(
            update={
                "seed": Seed(content=""),
                "is_hydrated": False,
                "column_hashes_": self.column_hashes,
                "derived_columns_to_types": self.columns_to_types
                if self.columns_to_types_ is None
                else None,
            }
        )

    def to_hydrated(self, content: str) -> SeedModel:
        """Creates a hydrated copy of this model with the given seed content.

        Returns:
            A hydrated copy of this model.
        """
        if self.is_hydrated:
            return self

        return self.copy(
            update={
                "seed": Seed(content=content),
                "is_hydrated": True,
                "column_hashes_": None,
            },
        )

    def is_breaking_change(self, previous: Model) -> t.Optional[bool]:
        if not isinstance(previous, SeedModel):
            return None

        new_columns = set(self.column_hashes)
        old_columns = set(previous.column_hashes)

        if not new_columns.issuperset(old_columns):
            return None

        for col in old_columns:
            if self.column_hashes[col] != previous.column_hashes[col]:
                return None

        return False

    def _ensure_hydrated(self) -> None:
        if not self.is_hydrated:
            raise SQLMeshError(f"Seed model '{self.name}' is not hydrated.")

    @cached_property
    def _reader(self) -> CsvSeedReader:
        return self.seed.reader(dialect=self.dialect, settings=self.kind.csv_settings)

    @property
    def _data_hash_values_no_sql(self) -> t.List[str]:
        data = super()._data_hash_values_no_sql
        for column_name, column_hash in self.column_hashes.items():
            data.append(column_name)
            data.append(column_hash)

        # Include grants in data hash for seed models to force recreation on grant changes
        # since seed models don't support migration
        data.append(json.dumps(self.grants, sort_keys=True) if self.grants else "")
        data.append(self.grants_target_layer)

        return data


class PythonModel(_Model):
    """The model definition which relies on a Python script to fetch the data.

    Args:
        entrypoint: The name of a Python function which contains the data fetching / transformation logic.
    """

    kind: ModelKind = FullKind()
    entrypoint: str
    source_type: t.Literal["python"] = "python"

    def validate_definition(self) -> None:
        super().validate_definition()

        if self.kind and not self.kind.supports_python_models:
            raise_config_error(
                f"Cannot create Python model '{self.name}' as the '{self.kind.name}' kind doesn't support Python models",
                self._path,
            )

    def render(
        self,
        *,
        context: ExecutionContext,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        **kwargs: t.Any,
    ) -> t.Iterator[QueryOrDF]:
        env = prepare_env(self.python_env)
        start, end = make_inclusive(start or c.EPOCH, end or c.EPOCH, self.dialect)
        execution_time = to_datetime(execution_time or c.EPOCH)

        variables = {
            **env.get(c.SQLMESH_VARS, {}),
            **env.get(c.SQLMESH_VARS_METADATA, {}),
            **kwargs.pop("variables", {}),
        }
        blueprint_variables = {
            k: d.parse_one(v.sql, dialect=self.dialect) if isinstance(v, SqlValue) else v
            for k, v in {
                **env.get(c.SQLMESH_BLUEPRINT_VARS, {}),
                **env.get(c.SQLMESH_BLUEPRINT_VARS_METADATA, {}),
            }.items()
        }
        try:
            kwargs = {
                **variables,
                **kwargs,
                "start": start,
                "end": end,
                "execution_time": execution_time,
                "latest": execution_time,  # TODO: Preserved for backward compatibility. Remove in 1.0.0.
            }
            df_or_iter = env[self.entrypoint](
                context=context.with_variables(variables, blueprint_variables=blueprint_variables),
                **kwargs,
            )

            if not isinstance(df_or_iter, types.GeneratorType):
                df_or_iter = [df_or_iter]

            for df in df_or_iter:
                yield df
        except Exception as e:
            raise PythonModelEvalError(format_evaluated_code_exception(e, self.python_env))

    def render_definition(
        self,
        include_python: bool = True,
        include_defaults: bool = False,
        render_query: bool = False,
    ) -> t.List[exp.Expr]:
        # Ignore the provided value for the include_python flag, since the Pyhon model's
        # definition without Python code is meaningless.
        return super().render_definition(
            include_python=True, include_defaults=include_defaults, render_query=render_query
        )

    @property
    def is_python(self) -> bool:
        return True

    def is_breaking_change(self, previous: Model) -> t.Optional[bool]:
        return None

    @property
    def _data_hash_values_no_sql(self) -> t.List[str]:
        data = super()._data_hash_values_no_sql
        data.append(self.entrypoint)
        return data


class ExternalModel(_Model):
    """The model definition which represents an external source/table."""

    kind: ModelKind = ExternalKind()
    source_type: t.Literal["external"] = "external"

    def is_breaking_change(self, previous: Model) -> t.Optional[bool]:
        if not isinstance(previous, ExternalModel):
            return None
        if not previous.columns_to_types_or_raise.items() - self.columns_to_types_or_raise.items():
            return False
        return None

    @property
    def depends_on(self) -> t.Set[str]:
        return set()

    @property
    def depends_on_self(self) -> bool:
        return False


Model = t.Union[SqlModel, SeedModel, PythonModel, ExternalModel]


class AuditResult(PydanticModel):
    audit: Audit
    """The audit this result is for."""
    audit_args: t.Dict[t.Any, t.Any]
    """Arguments passed to the audit."""
    model: t.Optional[_Model] = None
    """The model this audit is for."""
    count: t.Optional[int] = None
    """The number of records returned by the audit query. This could be None if the audit was skipped."""
    query: t.Optional[exp.Expr] = None
    """The rendered query used by the audit. This could be None if the audit was skipped."""
    skipped: bool = False
    """Whether or not the audit was blocking. This can be overriden by the user."""
    blocking: bool = True


class EvaluatableSignals(PydanticModel):
    signals_to_kwargs: t.Dict[str, t.Dict[str, t.Optional[exp.Expr]]]
    """A mapping of signal names to the kwargs passed to the signal."""
    python_env: t.Dict[str, Executable]
    """The Python environment that should be used to evaluated the rendered signal calls."""
    prepared_python_env: t.Dict[str, t.Any]
    """The prepared Python environment that should be used to evaluated the rendered signal calls."""


def _extract_blueprints(blueprints: t.Any, path: Path) -> t.List[t.Any]:
    if not blueprints:
        return [None]
    if isinstance(blueprints, exp.Paren):
        return [blueprints.unnest()]
    if isinstance(blueprints, (exp.Tuple, exp.Array)):
        return blueprints.expressions
    if isinstance(blueprints, list):
        return blueprints

    raise_config_error(
        "Expected a list or tuple consisting of key-value mappings for "
        f"the 'blueprints' property, got '{blueprints}' instead",
        path,
    )
    return []  # This is unreachable, but is done to satisfy mypy


def _extract_blueprint_variables(blueprint: t.Any, path: Path) -> t.Dict[str, t.Any]:
    if not blueprint:
        return {}
    if isinstance(blueprint, (exp.Paren, exp.PropertyEQ)):
        blueprint = blueprint.unnest()
        return {blueprint.left.name.lower(): blueprint.right}
    if isinstance(blueprint, (exp.Tuple, exp.Array)):
        return {e.left.name.lower(): e.right for e in blueprint.expressions}
    if isinstance(blueprint, dict):
        return {k.lower(): v for k, v in blueprint.items()}

    raise_config_error(
        f"Expected a key-value mapping for the blueprint value, got '{blueprint}' instead",
        path,
    )
    return {}  # This is unreachable, but is done to satisfy mypy


def create_models_from_blueprints(
    gateway: t.Optional[str | exp.Expr],
    blueprints: t.Any,
    get_variables: t.Callable[[t.Optional[str]], t.Dict[str, str]],
    loader: t.Callable[..., Model],
    path: Path = Path(),
    module_path: Path = Path(),
    dialect: DialectType = None,
    default_catalog_per_gateway: t.Optional[t.Dict[str, str]] = None,
    **loader_kwargs: t.Any,
) -> t.List[Model]:
    model_blueprints: t.List[Model] = []
    original_default_catalog = loader_kwargs.get("default_catalog")
    for blueprint in _extract_blueprints(blueprints, path):
        loader_kwargs["default_catalog"] = original_default_catalog
        blueprint_variables = _extract_blueprint_variables(blueprint, path)

        if gateway:
            rendered_gateway = render_expression(
                expression=exp.maybe_parse(gateway, dialect=dialect),
                module_path=module_path,
                macros=loader_kwargs.get("macros"),
                jinja_macros=loader_kwargs.get("jinja_macros"),
                path=path,
                dialect=dialect,
                default_catalog=loader_kwargs.get("default_catalog"),
                blueprint_variables=blueprint_variables,
            )
            gateway_name = rendered_gateway[0].name if rendered_gateway else None
        else:
            gateway_name = None

        if default_catalog_per_gateway and gateway_name:
            catalog = default_catalog_per_gateway.get(gateway_name)
            if catalog is not None:
                loader_kwargs["default_catalog"] = catalog
            else:
                # Gateway exists but has no entry in the dict (e.g., catalog-unsupported
                # engines like ClickHouse). Clear the default catalog so the global
                # default from the primary gateway doesn't leak into this model's name.
                loader_kwargs["default_catalog"] = None

        model_blueprints.append(
            loader(
                path=path,
                module_path=module_path,
                dialect=dialect,
                variables=get_variables(gateway_name),
                blueprint_variables=blueprint_variables,
                **loader_kwargs,
            )
        )

    return model_blueprints


def load_sql_based_models(
    expressions: t.List[exp.Expr],
    get_variables: t.Callable[[t.Optional[str]], t.Dict[str, str]],
    path: Path = Path(),
    module_path: Path = Path(),
    dialect: DialectType = None,
    default_catalog_per_gateway: t.Optional[t.Dict[str, str]] = None,
    **loader_kwargs: t.Any,
) -> t.List[Model]:
    gateway: t.Optional[exp.Expr] = None
    blueprints: t.Optional[exp.Expr] = None

    model_meta = seq_get(expressions, 0)
    for prop in (isinstance(model_meta, d.Model) and model_meta.expressions) or []:
        if prop.name == "gateway":
            gateway = prop.args["value"]
        elif prop.name == "blueprints":
            # We pop the `blueprints` here to avoid walking large lists when rendering the meta
            blueprints = prop.pop().args["value"]

    if isinstance(blueprints, d.MacroFunc):
        rendered_blueprints = render_expression(
            expression=blueprints,
            module_path=module_path,
            macros=loader_kwargs.get("macros"),
            jinja_macros=loader_kwargs.get("jinja_macros"),
            variables=get_variables(None),
            path=path,
            dialect=dialect,
            default_catalog=loader_kwargs.get("default_catalog"),
        )
        if not rendered_blueprints:
            raise_config_error("Failed to render blueprints property", path)

        # Help mypy see that rendered_blueprints can't be None
        assert rendered_blueprints

        if len(rendered_blueprints) > 1:
            rendered_blueprints = [exp.Tuple(expressions=rendered_blueprints)]

        blueprints = rendered_blueprints[0]

    return create_models_from_blueprints(
        gateway=gateway,
        blueprints=blueprints,
        get_variables=get_variables,
        loader=partial(load_sql_based_model, expressions),
        path=path,
        module_path=module_path,
        dialect=dialect,
        default_catalog_per_gateway=default_catalog_per_gateway,
        **loader_kwargs,
    )


def load_sql_based_model(
    expressions: t.List[exp.Expr],
    *,
    defaults: t.Optional[t.Dict[str, t.Any]] = None,
    path: t.Optional[Path] = None,
    module_path: Path = Path(),
    time_column_format: str = c.DEFAULT_TIME_COLUMN_FORMAT,
    macros: t.Optional[MacroRegistry] = None,
    jinja_macros: t.Optional[JinjaMacroRegistry] = None,
    audits: t.Optional[t.Dict[str, ModelAudit]] = None,
    python_env: t.Optional[t.Dict[str, Executable]] = None,
    dialect: t.Optional[str] = None,
    physical_schema_mapping: t.Optional[t.Dict[re.Pattern, str]] = None,
    default_catalog: t.Optional[str] = None,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    infer_names: t.Optional[bool] = False,
    blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
    **kwargs: t.Any,
) -> Model:
    """Load a model from a parsed SQLMesh model SQL file.

    Args:
        expressions: Model, *Statements, Query.
        defaults: Definition default values.
        path: An optional path to the model definition file.
        module_path: The python module path to serialize macros for.
        time_column_format: The default time column format to use if no model time column is configured.
        macros: The custom registry of macros. If not provided the default registry will be used.
        jinja_macros: The registry of Jinja macros.
        python_env: The custom Python environment for macros. If not provided the environment will be constructed
            from the macro registry.
        dialect: The default dialect if no model dialect is configured.
            The format must adhere to Python's strftime codes.
        physical_schema_mapping: A mapping of regular expressions to match against the model schema to produce the corresponding physical schema
        default_catalog: The default catalog if no model catalog is configured.
        variables: The variables to pass to the model.
        kwargs: Additional kwargs to pass to the loader.
    """
    missing_model_msg = f"""Please add a MODEL block at the top of the file. Example:

MODEL (
  name sqlmesh_example.full_model, --model name
  kind FULL, --materialization
  cron '@daily', --schedule
);

Learn more at https://sqlmesh.readthedocs.io/en/stable/concepts/models/overview
"""

    if not expressions:
        raise_config_error(missing_model_msg)

    dialect = dialect or ""
    meta = expressions[0]
    if not isinstance(meta, d.Model):
        if not infer_names:
            raise_config_error(missing_model_msg)
        meta = d.Model(expressions=[])  # Dummy meta node
        expressions.insert(0, meta)

    # We deliberately hold off rendering some properties at load time because there is not enough information available
    # at load time to render them. They will get rendered later at evaluation time
    unrendered_properties = {}
    unrendered_merge_filter = None

    for prop in meta.expressions:
        # Macro functions that programmaticaly generate the key-value pair properties should be rendered
        # This is needed in the odd case where a macro shares the name of one of the properties
        # eg `@session_properties()` Test: `test_macros_in_model_statement` Reference PR: #2574
        if isinstance(prop, d.MacroFunc):
            continue

        prop_name = prop.name.lower()
        if prop_name in {"signals", "audits"} | PROPERTIES:
            unrendered_properties[prop_name] = prop.args.get("value")
        elif (
            prop.name.lower() == "kind"
            and (value := prop.args.get("value"))
            and value.name.lower() == "incremental_by_unique_key"
        ):
            for kind_prop in value.expressions:
                if kind_prop.name.lower() == "merge_filter":
                    unrendered_merge_filter = kind_prop

    rendered_meta_exprs = render_expression(
        expression=meta,
        module_path=module_path,
        macros=macros,
        jinja_macros=jinja_macros,
        variables=variables,
        path=path,
        dialect=dialect,
        default_catalog=default_catalog,
        blueprint_variables=blueprint_variables,
    )

    if rendered_meta_exprs is None or len(rendered_meta_exprs) != 1:
        raise_config_error(
            f"Invalid MODEL statement:\n{meta.sql(dialect=dialect, pretty=True)}",
            path,
        )
        raise

    rendered_meta = rendered_meta_exprs[0]

    rendered_defaults = (
        render_model_defaults(
            defaults=defaults,
            module_path=module_path,
            macros=macros,
            jinja_macros=jinja_macros,
            variables=variables,
            path=path,
            dialect=dialect,
            default_catalog=default_catalog,
        )
        if defaults
        else {}
    )

    rendered_defaults = parse_defaults_properties(rendered_defaults, dialect=dialect)

    # Extract the query and any pre/post statements
    query_or_seed_insert, pre_statements, post_statements, on_virtual_update, inline_audits = (
        _split_sql_model_statements(expressions[1:], path, dialect=dialect)
    )

    meta_fields: t.Dict[str, t.Any] = {
        "dialect": dialect,
        "description": (
            "\n".join(comment.strip() for comment in rendered_meta.comments)
            if rendered_meta.comments
            else None
        ),
        **{prop.name.lower(): prop.args.get("value") for prop in rendered_meta.expressions},
        **kwargs,
    }

    # Discard the potentially half-rendered versions of these properties and replace them with the
    # original unrendered versions. They will get rendered properly at evaluation time
    meta_fields.update(unrendered_properties)

    if unrendered_merge_filter:
        for idx, kind_prop in enumerate(meta_fields["kind"].expressions):
            if kind_prop.name.lower() == "merge_filter":
                meta_fields["kind"].expressions[idx] = unrendered_merge_filter

    if isinstance(meta_fields.get("dialect"), exp.Expr):
        meta_fields["dialect"] = meta_fields["dialect"].name

    # The name of the model will be inferred from its path relative to `models/`, if it's not explicitly specified
    name = meta_fields.pop("name", "")
    if not name and infer_names:
        if path is None:
            raise ValueError(f"Model {name} must have a name")
        name = get_model_name(path)

    if not name:
        raise_config_error(
            "Please add the required 'name' field to the MODEL block at the top of the file.\n\n"
            + "Learn more at https://sqlmesh.readthedocs.io/en/stable/concepts/models/overview"
        )
    if "default_catalog" in meta_fields:
        raise_config_error(
            "`default_catalog` cannot be set on a per-model basis. It must be set at the connection level.",
            path,
        )

    common_kwargs = dict(
        pre_statements=pre_statements,
        post_statements=post_statements,
        on_virtual_update=on_virtual_update,
        defaults=rendered_defaults,
        path=path,
        module_path=module_path,
        macros=macros,
        python_env=python_env,
        jinja_macros=jinja_macros,
        physical_schema_mapping=physical_schema_mapping,
        default_catalog=default_catalog,
        variables=variables,
        inline_audits=inline_audits,
        blueprint_variables=blueprint_variables,
        use_original_sql=True,
        **meta_fields,
    )

    kind = common_kwargs.pop("kind", ModelMeta.all_field_infos()["kind"].default)

    if kind.name != ModelKindName.SEED:
        return create_sql_model(
            name,
            query_or_seed_insert,
            kind=kind,
            time_column_format=time_column_format,
            **common_kwargs,
        )

    seed_properties = {p.name.lower(): p.args.get("value") for p in kind.expressions}
    return create_seed_model(
        name,
        SeedKind(**seed_properties),
        **common_kwargs,
    )


def create_sql_model(
    name: TableName,
    query: t.Optional[exp.Expr],
    **kwargs: t.Any,
) -> Model:
    """Creates a SQL model.

    Args:
        name: The name of the model, which is of the form [catalog].[db].table.
            The catalog and db are optional.
        query: The model's logic in a form of a SELECT query.
    """
    if not isinstance(query, (exp.Query, d.JinjaQuery, d.MacroFunc)):
        raise_config_error(
            "A query is required and must be a SELECT statement, a UNION statement, or a JINJA_QUERY block",
            kwargs.get("path"),
        )
        assert isinstance(query, (exp.Query, d.JinjaQuery, d.MacroFunc))

    return _create_model(SqlModel, name, query=query, **kwargs)


def create_seed_model(
    name: TableName,
    seed_kind: SeedKind,
    *,
    path: t.Optional[Path] = None,
    module_path: Path = Path(),
    **kwargs: t.Any,
) -> Model:
    """Creates a Seed model.

    Args:
        name: The name of the model, which is of the form [catalog].[db].table.
            The catalog and db are optional.
        seed_kind: The information about the location of a seed and other related configuration.
        path: An optional path to the model definition file.
            from the macro registry.
    """
    seed_path = Path(seed_kind.path)
    marker, *subdirs = seed_path.parts
    if marker.lower() == "$root":
        seed_path = module_path.joinpath(*subdirs)
        seed_kind.path = str(seed_path)
    elif not seed_path.is_absolute():
        if path is None:
            seed_path = seed_path
        elif path.is_dir():
            seed_path = path / seed_path
        else:
            seed_path = path.parent / seed_path

    seed = create_seed(seed_path)

    return _create_model(
        SeedModel,
        name,
        path=path,
        seed=seed,
        kind=seed_kind,
        depends_on=kwargs.pop("depends_on", None),
        module_path=module_path,
        **kwargs,
    )


def create_python_model(
    name: str,
    entrypoint: str,
    python_env: t.Dict[str, Executable],
    *,
    macros: t.Optional[MacroRegistry] = None,
    jinja_macros: t.Optional[JinjaMacroRegistry] = None,
    path: Path = Path(),
    module_path: Path = Path(),
    depends_on: t.Optional[t.Set[str]] = None,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
    **kwargs: t.Any,
) -> Model:
    """Creates a Python model.

    Args:
        name: The name of the model, which is of the form [catalog].[db].table.
            The catalog and db are optional.
        entrypoint: The name of a Python function which contains the data fetching / transformation logic.
        python_env: The Python environment of all objects referenced by the model implementation.
        path: An optional path to the model definition file.
        depends_on: The custom set of model's upstream dependencies.
        variables: The variables to pass to the model.
        blueprint_variables: The blueprint's variables to pass to the model.
    """
    # Find dependencies for python models by parsing code if they are not explicitly defined
    # Also remove self-references that are found

    dialect = kwargs.get("dialect")

    dependencies_unspecified = depends_on is None

    parsed_depends_on, referenced_variables = (
        parse_dependencies(
            python_env,
            entrypoint,
            strict_resolution=dependencies_unspecified,
            variables=variables,
            blueprint_variables=blueprint_variables,
        )
        if python_env is not None
        else (set(), set())
    )
    if dependencies_unspecified:
        depends_on = parsed_depends_on - {name}
    else:
        depends_on_rendered = render_expression(
            expression=exp.Array(
                expressions=[exp.maybe_parse(dep, dialect=dialect) for dep in depends_on or []]
            ),
            module_path=module_path,
            macros=macros,
            jinja_macros=jinja_macros,
            variables=variables,
            path=path,
            dialect=dialect,
            default_catalog=kwargs.get("default_catalog"),
        )
        depends_on = {
            dep.sql(dialect=dialect)
            for dep in t.cast(t.List[exp.Expr], depends_on_rendered)[0].expressions
        }

    used_variables = {k: v for k, v in (variables or {}).items() if k in referenced_variables}
    if used_variables:
        python_env[c.SQLMESH_VARS] = Executable.value(used_variables, sort_root_dict=True)

    return _create_model(
        PythonModel,
        name,
        path=path,
        depends_on=depends_on,
        entrypoint=entrypoint,
        python_env=python_env,
        macros=macros,
        jinja_macros=jinja_macros,
        module_path=module_path,
        variables=variables,
        blueprint_variables=blueprint_variables,
        **kwargs,
    )


def create_external_model(
    name: TableName,
    *,
    dialect: t.Optional[str] = None,
    path: Path = Path(),
    defaults: t.Optional[t.Dict[str, t.Any]] = None,
    **kwargs: t.Any,
) -> ExternalModel:
    """Creates an external model.

    Args:
        name: The name of the model, which is of the form [catalog].[db].table.
            The catalog and db are optional.
        dialect: The dialect to serialize.
        path: An optional path to the model definition file.
    """
    return t.cast(
        ExternalModel,
        _create_model(
            ExternalModel,
            name,
            defaults=defaults,
            dialect=dialect,
            path=path,
            kind=ModelKindName.EXTERNAL.value,
            **kwargs,
        ),
    )


def _create_model(
    klass: t.Type[_Model],
    name: TableName,
    *,
    defaults: t.Optional[t.Dict[str, t.Any]] = None,
    path: t.Optional[Path] = None,
    time_column_format: str = c.DEFAULT_TIME_COLUMN_FORMAT,
    jinja_macros: t.Optional[JinjaMacroRegistry] = None,
    jinja_macro_references: t.Optional[t.Set[MacroReference]] = None,
    depends_on: t.Optional[t.Set[str]] = None,
    dialect: t.Optional[str] = None,
    physical_schema_mapping: t.Optional[t.Dict[re.Pattern, str]] = None,
    python_env: t.Optional[t.Dict[str, Executable]] = None,
    audit_definitions: t.Optional[t.Dict[str, ModelAudit]] = None,
    inline_audits: t.Optional[t.Dict[str, ModelAudit]] = None,
    module_path: Path = Path(),
    macros: t.Optional[MacroRegistry] = None,
    signal_definitions: t.Optional[SignalRegistry] = None,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
    use_original_sql: bool = False,
    **kwargs: t.Any,
) -> Model:
    validate_extra_and_required_fields(
        klass,
        {"name", *kwargs} - {"grain", "table_properties"},
        "MODEL block",
        path,
    )

    for prop in PROPERTIES:
        kwargs[prop] = _resolve_properties((defaults or {}).get(prop), kwargs.get(prop))

    dialect = dialect or ""

    physical_schema_mapping = physical_schema_mapping or {}
    model_schema_name = exp.to_table(name, dialect=dialect).db
    physical_schema_override: t.Optional[str] = None

    for re_pattern, override_schema in physical_schema_mapping.items():
        if re.match(re_pattern, model_schema_name):
            physical_schema_override = override_schema
            break

    raw_kind = kwargs.pop("kind", None)
    if raw_kind:
        kwargs["kind"] = create_model_kind(raw_kind, dialect, defaults or {})

    defaults = {k: v for k, v in (defaults or {}).items() if k in klass.all_fields()}
    if not issubclass(klass, SqlModel):
        defaults.pop("optimize_query", None)

    statements: t.List[t.Union[exp.Expr, t.Tuple[exp.Expr, bool]]] = []

    if "query" in kwargs:
        statements.append(kwargs["query"])
        kwargs["query"] = ParsableSql.from_parsed_expression(
            kwargs["query"], dialect, use_meta_sql=use_original_sql
        )

    # Merge default statements with model-specific statements
    for statement_field in ["pre_statements", "post_statements", "on_virtual_update"]:
        if statement_field in defaults:
            kwargs[statement_field] = [
                exp.maybe_parse(stmt, dialect=dialect) for stmt in defaults[statement_field]
            ] + kwargs.get(statement_field, [])
        if statement_field in kwargs:
            # Macros extracted from these statements need to be treated as metadata only
            is_metadata = statement_field == "on_virtual_update"
            for stmt in kwargs[statement_field]:
                # Extract the expression if it's ParsableSql already
                expr = stmt.parse(dialect) if isinstance(stmt, ParsableSql) else stmt
                statements.append((expr, is_metadata))
            kwargs[statement_field] = [
                # this to retain the transaction information
                stmt
                if isinstance(stmt, ParsableSql)
                else ParsableSql.from_parsed_expression(
                    stmt, dialect, use_meta_sql=use_original_sql
                )
                for stmt in kwargs[statement_field]
            ]

    # This is done to allow variables like @gateway to be used in these properties
    # since rendering shifted from load time to run time.
    # Note: we check for Tuple since that's what we expect from _resolve_properties
    for property_name in PROPERTIES:
        property_values = kwargs.get(property_name)
        if isinstance(property_values, exp.Tuple):
            statements.extend(property_values.expressions)

    if isinstance(getattr(kwargs.get("kind"), "merge_filter", None), exp.Expr):
        statements.append(kwargs["kind"].merge_filter)

    jinja_macro_references, referenced_variables = extract_macro_references_and_variables(
        *(gen(e if isinstance(e, exp.Expr) else e[0]) for e in statements)
    )

    if jinja_macros:
        jinja_macros = (
            jinja_macros if jinja_macros.trimmed else jinja_macros.trim(jinja_macro_references)
        )
    else:
        jinja_macros = JinjaMacroRegistry()

    for jinja_macro in jinja_macros.root_macros.values():
        referenced_variables.update(
            extract_macro_references_and_variables(jinja_macro.definition)[1]
        )

    # Merge model-specific audits with default audits
    if default_audits := defaults.pop("audits", None):
        kwargs["audits"] = default_audits + d.extract_function_calls(kwargs.pop("audits", []))

    model = klass(
        name=name,
        **{
            **(defaults or {}),
            "jinja_macros": jinja_macros or JinjaMacroRegistry(),
            "dialect": dialect,
            "depends_on": depends_on,
            "physical_schema_override": physical_schema_override,
            **kwargs,
        },
    )

    audit_definitions = {
        **(audit_definitions or {}),
        **(inline_audits or {}),
    }

    used_audits: t.Set[str] = {audit_name for audit_name, _ in model.audits}

    audit_definitions = {
        audit_name: audit_definitions[audit_name]
        for audit_name in used_audits
        if audit_name in audit_definitions
    }

    model.audit_definitions.update(audit_definitions)

    # Any macro referenced in audits or signals needs to be treated as metadata-only
    statements.extend((audit.query, True) for audit in audit_definitions.values())  # type: ignore[misc]

    # Ensure that all audits referenced in the model are defined
    from sqlmesh.core.audit.builtin import BUILT_IN_AUDITS

    available_audits = BUILT_IN_AUDITS.keys() | model.audit_definitions.keys()
    for referenced_audit, audit_args in model.audits:
        if referenced_audit not in available_audits:
            raise_config_error(f"Audit '{referenced_audit}' is undefined", location=path)

        statements.extend(
            (audit_arg_expression, True) for audit_arg_expression in audit_args.values()
        )

    signal_definitions = signal_definitions or UniqueKeyDict("signals")

    for referenced_signal, kwargs in model.signals:
        if referenced_signal and referenced_signal not in signal_definitions:
            raise_config_error(f"Signal '{referenced_signal}' is undefined", location=path)

        statements.extend((signal_kwarg, True) for signal_kwarg in kwargs.values())

    python_env = make_python_env(
        statements,
        jinja_macro_references,
        module_path,
        macros or macro.get_registry(),
        variables=variables,
        referenced_variables=referenced_variables,
        path=path,
        python_env=python_env,
        strict_resolution=depends_on is None,
        blueprint_variables=blueprint_variables,
        dialect=dialect,
    )

    env: t.Dict[str, t.Tuple[t.Any, t.Optional[bool]]] = {}

    for signal_name, _ in model.signals:
        if signal_name and signal_name in signal_definitions:
            func = signal_definitions[signal_name].func
            setattr(func, c.SQLMESH_METADATA, True)
            build_env(func, env=env, name=signal_name, path=module_path)

    model.python_env.update(python_env)
    model.python_env.update(serialize_env(env, path=module_path))
    model._path = path
    model.set_time_format(time_column_format)

    return t.cast(Model, model)


INSERT_SEED_MACRO_CALL = d.parse_one("@INSERT_SEED()")


def _split_sql_model_statements(
    expressions: t.List[exp.Expr],
    path: t.Optional[Path],
    dialect: t.Optional[str] = None,
) -> t.Tuple[
    t.Optional[exp.Expr],
    t.List[exp.Expr],
    t.List[exp.Expr],
    t.List[exp.Expr],
    UniqueKeyDict[str, ModelAudit],
]:
    """Extracts the SELECT query from a sequence of expressions.

    Args:
        expressions: The list of all SQL statements in the model definition.

    Returns:
        A tuple containing the extracted SELECT query or the `@INSERT_SEED()` call, the statements before the it,
        the statements after it, and the inline audit definitions.

    Raises:
        ConfigError: If the model definition contains more than one SELECT query or `@INSERT_SEED()` call.
    """
    from sqlmesh.core.audit import ModelAudit, load_audit

    query_positions = []
    sql_statements = []
    on_virtual_update = []
    inline_audits: UniqueKeyDict[str, ModelAudit] = UniqueKeyDict("inline_audits")

    idx = 0
    length = len(expressions)
    while idx < length:
        expr = expressions[idx]

        if isinstance(expr, d.Audit):
            loaded_audit = load_audit([expr, expressions[idx + 1]], dialect=dialect)
            assert isinstance(loaded_audit, ModelAudit)
            inline_audits[loaded_audit.name] = loaded_audit
            idx += 2
        elif isinstance(expr, d.VirtualUpdateStatement):
            for statement in expr.expressions:
                on_virtual_update.append(statement)
            idx += 1
        else:
            if (
                isinstance(expr, (exp.Query, d.JinjaQuery))
                or expr == INSERT_SEED_MACRO_CALL
                or (
                    isinstance(expr, d.MacroFunc)
                    and (expr.this.name.lower() == "union" or length == 1)
                )
            ):
                query_positions.append((expr, idx))
            sql_statements.append(expr)
            idx += 1

    if not query_positions:
        return None, sql_statements, [], on_virtual_update, inline_audits

    if len(query_positions) > 1:
        raise_config_error("Only one SELECT query is allowed per model", path)

    query, pos = query_positions[0]
    return query, sql_statements[:pos], sql_statements[pos + 1 :], on_virtual_update, inline_audits


def _resolve_model_refs_to_physical_tables(
    value: exp.Expr, table_mapping: t.Dict[str, str], dialect: DialectType
) -> exp.Literal:
    """Resolve managed-model references in a property value to their physical table names.

    The value is a single table reference or a comma-separated list of them. Each reference that
    matches a managed model (via ``table_mapping``) is swapped for its physical ``db.table`` name;
    anything else (e.g. a raw source) is kept as written. Returns a single string literal so the
    property renders just like a hand-written value.
    """
    if isinstance(value, exp.Literal) and value.is_string:
        refs = value.this.split(",")
    else:
        refs = [value.sql(dialect=dialect)]

    def resolve(ref: str) -> str:
        table = exp.to_table(ref.strip(), dialect=dialect)
        physical = table_mapping.get(exp.table_name(table, identify=True))
        # Managed model -> physical table; otherwise keep the reference (just unquoted/normalized).
        return exp.table_name(
            exp.to_table(physical, dialect=dialect) if physical else table, identify=False
        )

    return exp.Literal.string(",".join(resolve(ref) for ref in refs if ref.strip()))


def _resolve_properties(
    default: t.Optional[t.Dict[str, t.Any]],
    provided: t.Optional[exp.Expr | t.Dict[str, t.Any]],
) -> t.Optional[exp.Expr]:
    if isinstance(provided, dict):
        properties = {k: exp.Literal.string(k).eq(v) for k, v in provided.items()}
    elif provided:
        if isinstance(provided, exp.Paren):
            provided = exp.Tuple(expressions=[provided.this])
        properties = {expr.this.name: expr for expr in provided}
    else:
        properties = {}

    for k, v in (default or {}).items():
        if k not in properties:
            properties[k] = exp.Literal.string(k).eq(v)
        elif properties[k].expression.sql().lower() in {"none", "null"}:
            del properties[k]

    if properties:
        return exp.Tuple(expressions=list(properties.values()))

    return None


def _list_of_calls_to_exp(value: t.List[t.Tuple[str, t.Dict[str, t.Any]]]) -> exp.Expr:
    return exp.Tuple(
        expressions=[
            exp.Anonymous(
                this=v[0],
                expressions=[
                    exp.EQ(this=exp.convert(left), expression=exp.convert(right))
                    for left, right in v[1].items()
                ],
            )
            for v in value
        ]
    )


def _is_projection(expr: exp.Expr) -> bool:
    parent = expr.parent
    return isinstance(parent, exp.Select) and expr.arg_key == "expressions"


def _single_expr_or_tuple(values: t.Sequence[exp.Expr]) -> exp.Expr | exp.Tuple:
    return values[0] if len(values) == 1 else exp.Tuple(expressions=values)


def _refs_to_sql(values: t.Any) -> exp.Expr:
    return exp.Tuple(expressions=values)


def render_meta_fields(
    fields: t.Dict[str, t.Any],
    module_path: Path,
    path: t.Optional[Path],
    jinja_macros: t.Optional[JinjaMacroRegistry],
    macros: t.Optional[MacroRegistry],
    dialect: DialectType,
    variables: t.Optional[t.Dict[str, t.Any]],
    default_catalog: t.Optional[str],
    blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
) -> t.Dict[str, t.Any]:
    def render_field_value(value: t.Any) -> t.Any:
        if isinstance(value, exp.Expr) or (isinstance(value, str) and "@" in value):
            expression = exp.maybe_parse(value, dialect=dialect)
            rendered_expr = render_expression(
                expression=expression,
                module_path=module_path,
                macros=macros,
                jinja_macros=jinja_macros,
                variables=variables,
                path=path,
                dialect=dialect,
                default_catalog=default_catalog,
                blueprint_variables=blueprint_variables,
            )
            if not rendered_expr:
                raise SQLMeshError(
                    f"Rendering `{expression.sql(dialect=dialect)}` did not return an expression"
                )

            if len(rendered_expr) != 1:
                raise SQLMeshError(
                    f"Rendering `{expression.sql(dialect=dialect)}` must return one result, but got {len(rendered_expr)}"
                )

            # For cases where a property is conditionally assigned
            if rendered_expr[0].sql().lower() in {"none", "null"}:
                return None

            return rendered_expr[0]

        return value

    for field_name, field_info in ModelMeta.all_field_infos().items():
        field = field_info.alias or field_name
        field_value = fields.get(field)

        # We don't want to parse python model cron="@..." kwargs (e.g. @daily) into MacroVar
        if (
            field == "cron"
            and isinstance(field_value, str)
            and field_value.lower() in CRON_SHORTCUTS
        ) or field_value is None:
            continue

        if field in RUNTIME_RENDERED_MODEL_FIELDS:
            fields[field] = parse_strings_with_macro_refs(field_value, dialect)
            continue

        if isinstance(field_value, dict):
            rendered_dict = {}
            for key, value in field_value.items():
                if key in RUNTIME_RENDERED_MODEL_FIELDS:
                    rendered_dict[key] = parse_strings_with_macro_refs(value, dialect)
                elif (
                    # don't parse kind auto_restatement_cron="@..." kwargs (e.g. @daily) into MacroVar
                    key == "auto_restatement_cron"
                    and isinstance(value, str)
                    and value.lower() in CRON_SHORTCUTS
                ):
                    rendered_dict[key] = value
                elif (rendered := render_field_value(value)) is not None:
                    rendered_dict[key] = rendered

            if rendered_dict:
                fields[field] = rendered_dict
            else:
                fields.pop(field)
        elif isinstance(field_value, list):
            rendered_list = [
                rendered
                for value in field_value
                if (rendered := render_field_value(value)) is not None
            ]
            if rendered_list:
                fields[field] = rendered_list
            else:
                fields.pop(field)
        else:
            rendered_field = render_field_value(field_value)
            if rendered_field is not None:
                fields[field] = rendered_field
            else:
                fields.pop(field)

    return fields


def render_model_defaults(
    defaults: t.Dict[str, t.Any],
    module_path: Path,
    path: t.Optional[Path],
    jinja_macros: t.Optional[JinjaMacroRegistry],
    macros: t.Optional[MacroRegistry],
    dialect: DialectType,
    variables: t.Optional[t.Dict[str, t.Any]],
    default_catalog: t.Optional[str],
) -> t.Dict[str, t.Any]:
    rendered_defaults = render_meta_fields(
        fields=defaults,
        module_path=module_path,
        macros=macros,
        jinja_macros=jinja_macros,
        variables=variables,
        path=path,
        dialect=dialect,
        default_catalog=default_catalog,
    )

    # Validate defaults that have macros are rendered to boolean
    for boolean in {"optimize_query", "allow_partials", "enabled"}:
        var = rendered_defaults.get(boolean)
        if var is not None and not isinstance(var, (exp.Boolean, bool)):
            raise ConfigError(f"Expected boolean for '{var}', got '{type(var)}' instead")

    # Validate the 'interval_unit' if present is an Interval Unit
    var = rendered_defaults.get("interval_unit")
    if isinstance(var, str):
        try:
            rendered_defaults["interval_unit"] = IntervalUnit(var)
        except ValueError as e:
            raise ConfigError(f"Invalid interval unit: {var}") from e

    return rendered_defaults


def parse_defaults_properties(
    defaults: t.Dict[str, t.Any], dialect: DialectType
) -> t.Dict[str, t.Any]:
    for prop in PROPERTIES:
        default_properties = defaults.get(prop)
        for key, value in (default_properties or {}).items():
            if isinstance(key, str) and d.SQLMESH_MACRO_PREFIX in str(value):
                defaults[prop][key] = exp.maybe_parse(value, dialect=dialect)

    return defaults


def render_expression(
    expression: exp.Expr,
    module_path: Path,
    path: t.Optional[Path],
    jinja_macros: t.Optional[JinjaMacroRegistry] = None,
    macros: t.Optional[MacroRegistry] = None,
    dialect: DialectType = None,
    variables: t.Optional[t.Dict[str, t.Any]] = None,
    default_catalog: t.Optional[str] = None,
    blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
) -> t.Optional[t.List[exp.Expr]]:
    meta_python_env = make_python_env(
        expressions=expression,
        jinja_macro_references=None,
        module_path=module_path,
        macros=macros or macro.get_registry(),
        variables=variables,
        path=path,
        blueprint_variables=blueprint_variables,
    )
    return ExpressionRenderer(
        expression,
        dialect,
        [],
        path=path,
        jinja_macro_registry=jinja_macros,
        python_env=meta_python_env,
        default_catalog=default_catalog,
        quote_identifiers=False,
        normalize_identifiers=False,
    ).render()


META_FIELD_CONVERTER: t.Dict[str, t.Callable] = {
    "start": lambda value: exp.Literal.string(value),
    "cron": lambda value: exp.Literal.string(value),
    "cron_tz": lambda value: exp.Literal.string(value),
    "partitioned_by_": _single_expr_or_tuple,
    "clustered_by": _single_expr_or_tuple,
    "depends_on_": lambda value: exp.Tuple(expressions=sorted(value)),
    "pre": _list_of_calls_to_exp,
    "post": _list_of_calls_to_exp,
    "audits": _list_of_calls_to_exp,
    "columns_to_types_": lambda value: exp.Schema(
        expressions=[exp.ColumnDef(this=exp.to_column(c), kind=t) for c, t in value.items()]
    ),
    "column_descriptions_": lambda value: exp.Schema(
        expressions=[exp.to_column(c).eq(d) for c, d in value.items()]
    ),
    "tags": single_value_or_tuple,
    "grains": _refs_to_sql,
    "references": _refs_to_sql,
    "physical_properties_": lambda value: value,
    "virtual_properties_": lambda value: value,
    "session_properties_": lambda value: value,
    "allow_partials": exp.convert,
    "signals": lambda values: exp.tuple_(
        *(
            exp.func(
                name, *(exp.PropertyEQ(this=exp.var(k), expression=v) for k, v in args.items())
            )
            if name
            else exp.Tuple(expressions=[exp.var(k).eq(v) for k, v in args.items()])
            for name, args in values
        )
    ),
    "formatting": str,
    "optimize_query": str,
    "virtual_environment_mode": lambda value: exp.Literal.string(value.value),
    "dbt_node_info_": lambda value: value.to_expression(),
    "grants_": lambda value: value,
    "grants_target_layer": lambda value: exp.Literal.string(value.value),
}


def get_model_name(path: Path) -> str:
    path_parts = list(path.parts[path.parts.index("models") + 1 : -1]) + [path.stem]
    return ".".join(path_parts[-3:])


# function applied to time column when automatically used for partitioning in INCREMENTAL_BY_TIME_RANGE models
def clickhouse_partition_func(
    column: exp.Expr, columns_to_types: t.Optional[t.Dict[str, exp.DataType]]
) -> exp.Expr:
    # `toMonday()` function accepts a Date or DateTime type column

    col_type = (columns_to_types and columns_to_types.get(column.name)) or exp.DataType.build(
        "UNKNOWN"
    )
    col_type_is_conformable = col_type.is_type(
        exp.DataType.Type.DATE,
        exp.DataType.Type.DATE32,
        exp.DataType.Type.DATETIME,
        exp.DataType.Type.DATETIME64,
    )

    #  if input column is already a conformable type, just pass the column
    if col_type_is_conformable:
        return exp.func("toMonday", column, dialect="clickhouse")

    # if input column type is not known, cast input to DateTime64
    if col_type.is_type(exp.DataType.Type.UNKNOWN):
        return exp.func(
            "toMonday",
            exp.cast(column, exp.DataType.build("DateTime64(9, 'UTC')", dialect="clickhouse")),
            dialect="clickhouse",
        )

    # if input column type is known but not conformable, cast input to DateTime64 and cast output back to original type
    return exp.cast(
        exp.func(
            "toMonday",
            exp.cast(column, exp.DataType.build("DateTime64(9, 'UTC')", dialect="clickhouse")),
            dialect="clickhouse",
        ),
        col_type,
    )


TIME_COL_PARTITION_FUNC = {"clickhouse": clickhouse_partition_func}
