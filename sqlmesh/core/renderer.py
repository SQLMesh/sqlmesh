from __future__ import annotations

import logging
import typing as t
from contextlib import contextmanager
from functools import partial
from pathlib import Path

from sqlglot import exp, Dialect
from sqlglot.errors import SqlglotError
from sqlglot.helper import ensure_list
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.simplify import simplify

from sqlmesh.core import constants as c
from sqlmesh.core import dialect as d
from sqlmesh.core.macros import MacroEvaluator, RuntimeStage
from sqlmesh.utils.date import (
    TimeLike,
    date_dict,
    make_inclusive,
    to_datetime,
    make_ts_exclusive,
    to_tstz,
)
from sqlmesh.utils.errors import (
    ConfigError,
    ParsetimeAdapterCallError,
    SQLMeshError,
    raise_config_error,
)
from sqlmesh.utils.jinja import JinjaMacroRegistry, extract_error_details
from sqlmesh.utils.metaprogramming import Executable, prepare_env

if t.TYPE_CHECKING:
    from sqlglot._typing import E
    from sqlglot.dialects.dialect import DialectType

    from sqlmesh.core.linter.rule import Rule
    from sqlmesh.core.model.definition import _Model
    from sqlmesh.core.snapshot import DeployabilityIndex, Snapshot


logger = logging.getLogger(__name__)


class BaseExpressionRenderer:
    def __init__(
        self,
        expression: exp.Expr,
        dialect: DialectType,
        macro_definitions: t.List[d.MacroDef],
        path: t.Optional[Path] = None,
        jinja_macro_registry: t.Optional[JinjaMacroRegistry] = None,
        python_env: t.Optional[t.Dict[str, Executable]] = None,
        only_execution_time: bool = False,
        schema: t.Optional[t.Dict[str, t.Any]] = None,
        default_catalog: t.Optional[str] = None,
        quote_identifiers: bool = True,
        normalize_identifiers: bool = True,
        optimize_query: t.Optional[bool] = True,
        model: t.Optional[_Model] = None,
    ):
        self._expression = expression
        self._dialect = dialect
        self._macro_definitions = macro_definitions
        self._path = path
        self._jinja_macro_registry = jinja_macro_registry or JinjaMacroRegistry()
        self._python_env = python_env or {}
        self._only_execution_time = only_execution_time
        self._default_catalog = default_catalog
        self._normalize_identifiers = normalize_identifiers
        self._quote_identifiers = quote_identifiers
        self.update_schema({} if schema is None else schema)
        self._cache: t.List[t.Optional[exp.Expr]] = []
        self._model_fqn = model.fqn if model else None
        self._optimize_query_flag = optimize_query is not False
        self._model = model

    def update_schema(self, schema: t.Dict[str, t.Any]) -> None:
        self.schema = d.normalize_mapping_schema(schema, dialect=self._dialect)

    def _render(
        self,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        table_mapping: t.Optional[t.Dict[str, str]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        runtime_stage: RuntimeStage = RuntimeStage.LOADING,
        **kwargs: t.Any,
    ) -> t.List[t.Optional[exp.Expr]]:
        """Renders a expression, expanding macros with provided kwargs

        Args:
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time.
            snapshots: All upstream snapshots (by model name) to use for expansion and mapping of physical locations.
            table_mapping: Table mapping of physical locations. Takes precedence over snapshot mappings.
            deployability_index: Determines snapshots that are deployable in the context of this evaluation.
            runtime_stage: Indicates the current runtime stage, for example if we're still loading the project, etc.
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            The rendered expressions.
        """

        should_cache = self._should_cache(
            runtime_stage, start, end, execution_time, *kwargs.values()
        )

        if should_cache and self._cache:
            return self._cache

        environment_naming_info = kwargs.get("environment_naming_info")
        if environment_naming_info is not None:
            kwargs["this_env"] = getattr(environment_naming_info, "name")
            if snapshots:
                schemas, views = set(), []
                for snapshot in snapshots.values():
                    if snapshot.is_model and not snapshot.is_symbolic:
                        schemas.add(
                            snapshot.qualified_view_name.schema_for_environment(
                                environment_naming_info, dialect=self._dialect
                            )
                        )
                        views.append(
                            snapshot.display_name(
                                environment_naming_info, self._default_catalog, self._dialect
                            )
                        )
                if schemas:
                    kwargs["schemas"] = list(schemas)
                if views:
                    kwargs["views"] = views

        this_model = kwargs.pop("this_model", None)

        this_snapshot = (snapshots or {}).get(self._model_fqn) if self._model_fqn else None
        if not this_model and self._model_fqn:
            this_model = self._resolve_table(
                self._model_fqn,
                snapshots={self._model_fqn: this_snapshot} if this_snapshot else None,
                deployability_index=deployability_index,
                table_mapping=table_mapping,
            )
        if this_snapshot and (kind := this_snapshot.model_kind_name):
            kwargs["model_kind_name"] = kind.name

        def _resolve_table(table: str | exp.Table) -> str:
            return self._resolve_table(
                d.normalize_model_name(table, self._default_catalog, self._dialect),
                snapshots=snapshots,
                table_mapping=table_mapping,
                deployability_index=deployability_index,
            ).sql(dialect=self._dialect, identify=True, comments=False)

        macro_evaluator = MacroEvaluator(
            self._dialect,
            python_env=self._python_env,
            schema=self.schema,
            runtime_stage=runtime_stage,
            resolve_table=_resolve_table,
            resolve_tables=lambda e: self._resolve_tables(
                e,
                snapshots=snapshots,
                table_mapping=table_mapping,
                deployability_index=deployability_index,
                start=start,
                end=end,
                execution_time=execution_time,
                runtime_stage=runtime_stage,
            ),
            snapshots=snapshots,
            default_catalog=self._default_catalog,
            path=self._path,
            environment_naming_info=environment_naming_info,
            model_fqn=self._model_fqn,
        )

        start_time, end_time = (
            make_inclusive(start or c.EPOCH, end or c.EPOCH, self._dialect)
            if not self._only_execution_time
            else (None, None)
        )

        render_kwargs = {
            **date_dict(
                to_datetime(execution_time or c.EPOCH),
                start_time,
                end_time,
            ),
            **kwargs,
        }

        if this_model:
            render_kwargs["this_model"] = this_model

        macro_evaluator.locals.update(render_kwargs)

        variables = kwargs.pop("variables", {})
        if variables:
            macro_evaluator.locals.setdefault(c.SQLMESH_VARS, {}).update(variables)

        expressions: t.List[exp.Expr] = [self._expression]
        if isinstance(self._expression, d.Jinja):
            try:
                jinja_env_kwargs = {
                    **{
                        **render_kwargs,
                        **_prepare_python_env_for_jinja(macro_evaluator, self._python_env),
                        **variables,
                    },
                    "snapshots": snapshots or {},
                    "table_mapping": table_mapping,
                    "deployability_index": deployability_index,
                    "default_catalog": self._default_catalog,
                    "runtime_stage": runtime_stage.value,
                    "resolve_table": _resolve_table,
                    "model_instance": self._model,
                }

                if this_model:
                    jinja_env_kwargs["this_model"] = this_model.sql(
                        dialect=self._dialect, identify=True, comments=False
                    )

                if self._model and self._model.kind.is_incremental_by_time_range:
                    all_refs = list(
                        self._jinja_macro_registry.global_objs.get("sources", {}).values()  # type: ignore
                    ) + list(
                        self._jinja_macro_registry.global_objs.get("refs", {}).values()  # type: ignore
                    )
                    for ref in all_refs:
                        if ref.event_time_filter:
                            ref.event_time_filter["start"] = render_kwargs["start_tstz"]
                            ref.event_time_filter["end"] = to_tstz(
                                make_ts_exclusive(render_kwargs["end_tstz"], dialect=self._dialect)
                            )

                jinja_env = self._jinja_macro_registry.build_environment(**jinja_env_kwargs)

                expressions = []
                rendered_expression = jinja_env.from_string(self._expression.name).render()
                logger.debug(
                    f"Rendered Jinja expression for model '{self._model_fqn}' at '{self._path}': '{rendered_expression}'"
                )
            except ParsetimeAdapterCallError:
                raise
            except Exception as ex:
                raise ConfigError(
                    f"Could not render jinja for '{self._path}'.\n" + extract_error_details(ex)
                ) from ex

            if rendered_expression.strip():
                # ensure there is actual SQL and not just comments and non-SQL jinja
                dialect = Dialect.get_or_raise(self._dialect)
                tokens = dialect.tokenize(rendered_expression)

                if tokens:
                    try:
                        expressions = [
                            e for e in dialect.parser().parse(tokens, rendered_expression) if e
                        ]

                        if not expressions:
                            raise ConfigError(
                                f"Failed to parse an expression:\n{rendered_expression}"
                            )
                    except Exception as ex:
                        raise ConfigError(
                            f"Could not parse the rendered jinja at '{self._path}'.\n{ex}"
                        ) from ex

        for definition in self._macro_definitions:
            try:
                macro_evaluator.evaluate(definition)
            except Exception as ex:
                raise_config_error(
                    f"Failed to evaluate macro '{definition}'.\n\n{ex}\n", self._path
                )

        resolved_expressions: t.List[t.Optional[exp.Expr]] = []

        for expression in expressions:
            try:
                transformed_expressions = ensure_list(macro_evaluator.transform(expression))
            except Exception as ex:
                raise_config_error(
                    f"Failed to resolve macros for\n\n{expression.sql(dialect=self._dialect, pretty=True)}\n\n{ex}\n",
                    self._path,
                )

            for expression in t.cast(t.List[exp.Expr], transformed_expressions):
                with self._normalize_and_quote(expression) as expression:
                    if hasattr(expression, "selects"):
                        for select in expression.selects:
                            if not isinstance(select, exp.Alias) and select.output_name not in (
                                "*",
                                "",
                            ):
                                alias = exp.alias_(
                                    select, select.output_name, quoted=self._quote_identifiers
                                )
                                comments = alias.this.comments
                                if comments:
                                    alias.add_comments(comments)
                                    comments.clear()

                                select.replace(alias)
                resolved_expressions.append(expression)

        # We dont cache here if columns_to_type was called in a macro.
        # This allows the model's query to be re-rendered so that the
        # MacroEvaluator can resolve columns_to_types calls and provide true schemas.
        if should_cache and (not self.schema.empty or not macro_evaluator.columns_to_types_called):
            self._cache = resolved_expressions
        return resolved_expressions

    def update_cache(self, expression: t.Optional[exp.Expr]) -> None:
        self._cache = [expression]

    def _resolve_table(
        self,
        table_name: str | exp.Expr,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        table_mapping: t.Optional[t.Dict[str, str]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
    ) -> exp.Table:
        table = exp.replace_tables(
            t.cast(exp.Table, exp.maybe_parse(table_name, into=exp.Table, dialect=self._dialect)),
            {
                **self._to_table_mapping((snapshots or {}).values(), deployability_index),
                **(table_mapping or {}),
            },
            dialect=self._dialect,
            copy=False,
        )
        # We quote the table here to mimic the behavior of _resolve_tables, otherwise we may end
        # up normalizing twice, because _to_table_mapping returns the mapped names unquoted.
        return (
            d.quote_identifiers(table, dialect=self._dialect) if self._quote_identifiers else table
        )

    def _resolve_tables(
        self,
        expression: E,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        table_mapping: t.Optional[t.Dict[str, str]] = None,
        expand: t.Iterable[str] = tuple(),
        deployability_index: t.Optional[DeployabilityIndex] = None,
        **kwargs: t.Any,
    ) -> E:
        if not snapshots and not table_mapping and not expand:
            return expression

        expression = expression.copy()
        with self._normalize_and_quote(expression) as expression:
            snapshots = snapshots or {}
            table_mapping = table_mapping or {}
            mapping = {
                **self._to_table_mapping(snapshots.values(), deployability_index),
                **table_mapping,
            }
            expand = set(expand) | {
                name for name, snapshot in snapshots.items() if snapshot.is_embedded
            }

            if expand:
                model_mapping = {
                    name: snapshot.model
                    for name, snapshot in snapshots.items()
                    if snapshot.is_model
                }

                def _expand(node: exp.Expr) -> exp.Expr:
                    if isinstance(node, exp.Table) and snapshots:
                        name = exp.table_name(node, identify=True)
                        model = model_mapping.get(name)
                        if (
                            name in expand
                            and model
                            and not model.is_seed
                            and not model.kind.is_external
                        ):
                            nested_query = model.render_query(
                                start=start,
                                end=end,
                                execution_time=execution_time,
                                snapshots=snapshots,
                                table_mapping=table_mapping,
                                expand=expand,
                                deployability_index=deployability_index,
                                **kwargs,
                            )
                            if nested_query is not None:
                                return nested_query.subquery(
                                    alias=node.alias or model.view_name,
                                    copy=False,
                                )
                            logger.warning("Failed to expand the nested model '%s'", name)
                    return node

                expression = expression.transform(_expand, copy=False)  # type: ignore

            if mapping:
                expression = exp.replace_tables(
                    expression, mapping, dialect=self._dialect, copy=False
                )

            return expression

    @contextmanager
    def _normalize_and_quote(self, query: E) -> t.Iterator[E]:
        if self._normalize_identifiers:
            with d.normalize_and_quote(
                query, self._dialect, self._default_catalog, quote=self._quote_identifiers
            ) as query:
                yield query
        else:
            yield query

    def _should_cache(self, runtime_stage: RuntimeStage, *args: t.Any) -> bool:
        return runtime_stage == RuntimeStage.LOADING and not any(args)

    def _to_table_mapping(
        self, snapshots: t.Iterable[Snapshot], deployability_index: t.Optional[DeployabilityIndex]
    ) -> t.Dict[str, str]:
        from sqlmesh.core.snapshot import to_table_mapping

        return to_table_mapping(snapshots, deployability_index)


class ExpressionRenderer(BaseExpressionRenderer):
    def render(
        self,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        table_mapping: t.Optional[t.Dict[str, str]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        expand: t.Iterable[str] = tuple(),
        **kwargs: t.Any,
    ) -> t.Optional[t.List[exp.Expr]]:
        try:
            expressions = super()._render(
                start=start,
                end=end,
                execution_time=execution_time,
                snapshots=snapshots,
                deployability_index=deployability_index,
                table_mapping=table_mapping,
                **kwargs,
            )
        except ParsetimeAdapterCallError:
            return None

        return [
            self._resolve_tables(
                e,
                snapshots=snapshots,
                table_mapping=table_mapping,
                expand=expand,
                deployability_index=deployability_index,
                start=start,
                end=end,
                execution_time=execution_time,
                **kwargs,
            )
            for e in expressions
            if e and not isinstance(e, exp.Semicolon)
        ]


def render_statements(
    statements: t.List[str],
    dialect: str,
    default_catalog: t.Optional[str] = None,
    python_env: t.Optional[t.Dict[str, Executable]] = None,
    jinja_macros: t.Optional[JinjaMacroRegistry] = None,
    **render_kwargs: t.Any,
) -> t.List[str]:
    rendered_statements: t.List[str] = []
    for statement in statements:
        for expression in d.parse(statement, default_dialect=dialect):
            if expression:
                rendered = ExpressionRenderer(
                    expression,
                    dialect,
                    [],
                    jinja_macro_registry=jinja_macros,
                    python_env=python_env,
                    default_catalog=default_catalog,
                    quote_identifiers=False,
                    normalize_identifiers=False,
                ).render(**render_kwargs)

                if not rendered:
                    # Warning instead of raising for cases where a statement is conditionally executed
                    logger.warning(
                        f"Rendering `{expression.sql(dialect=dialect)}` did not return an expression"
                    )
                else:
                    rendered_statements.extend(expr.sql(dialect=dialect) for expr in rendered)

    return rendered_statements


class QueryRenderer(BaseExpressionRenderer):
    def __init__(self, *args: t.Any, **kwargs: t.Any):
        super().__init__(*args, **kwargs)
        self._optimized_cache: t.Optional[exp.Query] = None
        self._violated_rules: t.Dict[type[Rule], t.Any] = {}

    def update_schema(self, schema: t.Dict[str, t.Any]) -> None:
        super().update_schema(schema)
        self._optimized_cache = None

    def render(
        self,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        table_mapping: t.Optional[t.Dict[str, str]] = None,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        expand: t.Iterable[str] = tuple(),
        needs_optimization: bool = True,
        runtime_stage: RuntimeStage = RuntimeStage.LOADING,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Query]:
        """Renders a query, expanding macros with provided kwargs, and optionally expanding referenced models.

        Args:
            query: The query to render.
            start: The start datetime to render. Defaults to epoch start.
            end: The end datetime to render. Defaults to epoch start.
            execution_time: The date/time time reference to use for execution time. Defaults to epoch start.
            snapshots: All upstream snapshots (by model name) to use for expansion and mapping of physical locations.
            table_mapping: Table mapping of physical locations. Takes precedence over snapshot mappings.
            deployability_index: Determines snapshots that are deployable in the context of this evaluation.
            expand: Expand referenced models as subqueries. This is used to bypass backfills when running queries
                that depend on materialized tables.  Model definitions are inlined and can thus be run end to
                end on the fly.
            needs_optimization: Whether or not an optimization should be attempted
                (if passing False, it still may return a cached optimized query).
            runtime_stage: Indicates the current runtime stage, for example if we're still loading the project, etc.
            kwargs: Additional kwargs to pass to the renderer.

        Returns:
            The rendered expression.
        """

        should_cache = self._should_cache(
            runtime_stage, start, end, execution_time, *kwargs.values()
        )

        if should_cache and self._optimized_cache:
            query = self._optimized_cache
        else:
            try:
                expressions = super()._render(
                    start=start,
                    end=end,
                    execution_time=execution_time,
                    snapshots=snapshots,
                    table_mapping=table_mapping,
                    deployability_index=deployability_index,
                    runtime_stage=runtime_stage,
                    **kwargs,
                )
            except ParsetimeAdapterCallError:
                return None

            expressions = [e for e in expressions if not isinstance(e, exp.Semicolon)]

            if not expressions:
                # We assume that if there are no expressions, then the model contains dynamic Jinja SQL
                # and we thus treat it similar to models with adapter calls to match dbt's behavior.
                if isinstance(self._expression, d.JinjaQuery):
                    return None

                raise ConfigError(f"Failed to render query at '{self._path}':\n{self._expression}")

            if len(expressions) > 1:
                raise ConfigError(f"Too many statements in query:\n{self._expression}")

            query = expressions[0]  # type: ignore

            if not query:
                return None
            if not isinstance(query, exp.Query):
                raise_config_error(
                    f"Model query needs to be a SELECT or a UNION, got {query}.", self._path
                )
                raise

            if needs_optimization and self._optimize_query_flag:
                deps = d.find_tables(
                    query, default_catalog=self._default_catalog, dialect=self._dialect
                )

                query = self._optimize_query(query, deps)

                if should_cache:
                    self._optimized_cache = query

        if needs_optimization:
            query = self._resolve_tables(
                query,
                snapshots=snapshots,
                table_mapping=table_mapping,
                expand=expand,
                deployability_index=deployability_index,
                start=start,
                end=end,
                execution_time=execution_time,
                runtime_stage=runtime_stage,
                **kwargs,
            )

        return query

    def update_cache(
        self,
        expression: t.Optional[exp.Expr],
        violated_rules: t.Optional[t.Dict[type[Rule], t.Any]] = None,
        optimized: bool = False,
    ) -> None:
        if optimized:
            if not isinstance(expression, exp.Query):
                raise SQLMeshError(f"Expected a Query but got: {expression}")
            self._optimized_cache = expression
        else:
            super().update_cache(expression)

        self._violated_rules = violated_rules or {}

    def _optimize_query(self, query: exp.Query, all_deps: t.Set[str]) -> exp.Query:
        from sqlmesh.core.linter.rules.builtin import (
            AmbiguousOrInvalidColumn,
            InvalidSelectStarExpansion,
        )

        # We don't want to normalize names in the schema because that's handled by the optimizer
        original = query
        missing_deps = set()
        all_deps = all_deps - {self._model_fqn}
        should_optimize = not self.schema.empty or not all_deps

        for dep in all_deps:
            if not self.schema.find(exp.to_table(dep)):
                should_optimize = False
                missing_deps.add(dep)

        if self._model_fqn and not should_optimize and any(s.is_star for s in query.selects):
            deps = ", ".join(f"'{dep}'" for dep in sorted(missing_deps))
            self._violated_rules[InvalidSelectStarExpansion] = deps

        try:
            if should_optimize:
                query = query.copy()
                simplify(
                    annotate_types(
                        qualify(
                            query,
                            dialect=self._dialect,
                            schema=self.schema,
                            infer_schema=False,
                            catalog=self._default_catalog,
                            quote_identifiers=self._quote_identifiers,
                        ),
                        schema=self.schema,
                        dialect=self._dialect,
                    ),
                    dialect=self._dialect,
                )
        except SqlglotError as ex:
            self._violated_rules[AmbiguousOrInvalidColumn] = ex

            query = original

        except Exception as ex:
            raise_config_error(
                f"Failed to optimize query, please file an issue at https://github.com/SQLMesh/sqlmesh/issues/new. {ex}",
                self._path,
            )

        if not query.type:
            for select in query.expressions:
                annotate_types(select)

        return query


def _prepare_python_env_for_jinja(
    evaluator: MacroEvaluator,
    python_env: t.Dict[str, Executable],
) -> t.Dict[str, t.Any]:
    prepared_env = prepare_env(python_env)
    # Pass the evaluator to all macro functions
    return {
        key: partial(value, evaluator) if callable(value) else value
        for key, value in prepared_env.items()
    }
