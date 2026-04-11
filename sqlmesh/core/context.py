"""
# Context

A SQLMesh context encapsulates a SQLMesh environment. When you create a new context, it will discover and
load your project's models, macros, and audits. Afterwards, you can use the context to create and apply
plans, visualize your model's lineage, run your audits and model tests, and perform various other tasks.
For more information regarding what a context can do, see `sqlmesh.core.context.Context`.

# Examples:

Creating and applying a plan against the staging environment.
```python
from sqlmesh.core.context import Context
context = Context(paths="example", config="local_config")
plan = context.plan("staging")
context.apply(plan)
```

Running audits on your data.
```python
from sqlmesh.core.context import Context
context = Context(paths="example", config="local_config")
context.audit("yesterday", "now")
```

Running tests on your models.
```python
from sqlmesh.core.context import Context
context = Context(paths="example")
context.test()
```
"""

from __future__ import annotations

import abc
import collections
import logging
import sys
import time
import traceback
import typing as t
from functools import cached_property
from io import StringIO
from itertools import chain
from pathlib import Path
from shutil import rmtree
from types import MappingProxyType
from datetime import datetime

from sqlglot import Dialect, exp
from sqlglot.helper import first
from sqlglot.lineage import GraphHTML

from sqlmesh.core import analytics
from sqlmesh.core import constants as c
from sqlmesh.core.analytics import python_api_analytics
from sqlmesh.core.audit import Audit, ModelAudit, StandaloneAudit
from sqlmesh.core.config import (
    CategorizerConfig,
    Config,
    load_configs,
)
from sqlmesh.core.config.connection import ConnectionConfig
from sqlmesh.core.config.loader import C
from sqlmesh.core.config.root import RegexKeyDict
from sqlmesh.core.console import get_console
from sqlmesh.core.context_diff import ContextDiff
from sqlmesh.core.dialect import (
    format_model_expressions,
    is_meta_expression,
    normalize_model_name,
    pandas_to_sql,
    parse,
    parse_one,
)
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.environment import Environment, EnvironmentNamingInfo, EnvironmentStatements
from sqlmesh.core.loader import Loader
from sqlmesh.core.linter.definition import AnnotatedRuleViolation, Linter
from sqlmesh.core.linter.rules import BUILTIN_RULES
from sqlmesh.core.macros import ExecutableOrMacro, macro
from sqlmesh.core.metric import Metric, rewrite
from sqlmesh.core.model import Model, update_model_schemas
from sqlmesh.core.config.model import ModelDefaultsConfig
from sqlmesh.core.notification_target import (
    NotificationEvent,
    NotificationTarget,
    NotificationTargetManager,
)
from sqlmesh.core.plan import Plan, PlanBuilder, SnapshotIntervals, PlanExplainer
from sqlmesh.core.plan.definition import UserProvidedFlags
from sqlmesh.core.reference import ReferenceGraph
from sqlmesh.core.scheduler import Scheduler, CompletionStatus
from sqlmesh.core.schema_loader import create_external_models_file
from sqlmesh.core.selector import Selector, NativeSelector
from sqlmesh.core.snapshot import (
    DeployabilityIndex,
    Snapshot,
    SnapshotEvaluator,
    SnapshotFingerprint,
    missing_intervals,
    to_table_mapping,
)
from sqlmesh.core.snapshot.definition import get_next_model_interval_start
from sqlmesh.core.state_sync import (
    CachingStateSync,
    StateReader,
    StateSync,
)
from sqlmesh.core.janitor import cleanup_expired_views, delete_expired_snapshots
from sqlmesh.core.table_diff import TableDiff
from sqlmesh.core.test import (
    ModelTextTestResult,
    ModelTestMetadata,
    generate_test,
    run_tests,
    filter_tests_by_patterns,
)
from sqlmesh.core.user import User
from sqlmesh.utils import UniqueKeyDict, Verbosity
from sqlmesh.utils.concurrency import concurrent_apply_to_values
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.date import (
    TimeLike,
    to_timestamp,
    format_tz_datetime,
    now_timestamp,
    now,
    to_datetime,
    make_exclusive,
)
from sqlmesh.utils.errors import (
    CircuitBreakerError,
    ConfigError,
    PlanError,
    SQLMeshError,
    UncategorizedPlanError,
    LinterError,
)
from sqlmesh.utils.config import print_config
from sqlmesh.utils.jinja import JinjaMacroRegistry
from sqlmesh.utils.windows import IS_WINDOWS, fix_windows_path

if t.TYPE_CHECKING:
    import pandas as pd
    from typing_extensions import Literal

    from sqlmesh.core.engine_adapter._typing import (
        BigframeSession,
        DF,
        PySparkDataFrame,
        PySparkSession,
        SnowparkSession,
    )
    from sqlmesh.core.snapshot import Node

    from sqlmesh.core.snapshot.definition import Intervals

    ModelOrSnapshot = t.Union[str, Model, Snapshot]
    NodeOrSnapshot = t.Union[str, Model, StandaloneAudit, Snapshot]

logger = logging.getLogger(__name__)


class BaseContext(abc.ABC):
    """The base context which defines methods to execute a model."""

    @property
    @abc.abstractmethod
    def default_dialect(self) -> t.Optional[str]:
        """Returns the default dialect."""

    @property
    @abc.abstractmethod
    def _model_tables(self) -> t.Dict[str, str]:
        """Returns a mapping of model names to tables."""

    @property
    @abc.abstractmethod
    def engine_adapter(self) -> EngineAdapter:
        """Returns an engine adapter."""

    @property
    def spark(self) -> t.Optional[PySparkSession]:
        """Returns the spark session if it exists."""
        return self.engine_adapter.spark

    @property
    def snowpark(self) -> t.Optional[SnowparkSession]:
        """Returns the snowpark session if it exists."""
        return self.engine_adapter.snowpark

    @property
    def bigframe(self) -> t.Optional[BigframeSession]:
        """Returns the bigframe session if it exists."""
        return self.engine_adapter.bigframe

    @property
    def default_catalog(self) -> t.Optional[str]:
        raise NotImplementedError

    def table(self, model_name: str) -> str:
        get_console().log_warning(
            "The SQLMesh context's `table` method is deprecated and will be removed "
            "in a future release. Please use the `resolve_table` method instead."
        )
        return self.resolve_table(model_name)

    def resolve_table(self, model_name: str) -> str:
        """Gets the physical table name for a given model.

        Args:
            model_name: The model name.

        Returns:
            The physical table name.
        """
        model_name = normalize_model_name(model_name, self.default_catalog, self.default_dialect)

        if model_name not in self._model_tables:
            model_name_list = "\n".join(list(self._model_tables))
            logger.debug(
                f"'{model_name}' not found in model to table mapping. Available model names: \n{model_name_list}"
            )
            raise SQLMeshError(
                f"Unable to find a table mapping for model '{model_name}'. Has it been spelled correctly?"
            )

        # We generate SQL for the default dialect because the table name may be used in a
        # fetchdf call and so the quotes need to be correct (eg. backticks for bigquery)
        return parse_one(self._model_tables[model_name]).sql(
            dialect=self.default_dialect, identify=True
        )

    def fetchdf(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> pd.DataFrame:
        """Fetches a dataframe given a sql string or sqlglot expression.

        Args:
            query: SQL string or sqlglot expression.
            quote_identifiers: Whether to quote all identifiers in the query.

        Returns:
            The default dataframe is Pandas, but for Spark a PySpark dataframe is returned.
        """
        return self.engine_adapter.fetchdf(query, quote_identifiers=quote_identifiers)

    def fetch_pyspark_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> PySparkDataFrame:
        """Fetches a PySpark dataframe given a sql string or sqlglot expression.

        Args:
            query: SQL string or sqlglot expression.
            quote_identifiers: Whether to quote all identifiers in the query.

        Returns:
            A PySpark dataframe.
        """
        return self.engine_adapter.fetch_pyspark_df(query, quote_identifiers=quote_identifiers)


class ExecutionContext(BaseContext):
    """The minimal context needed to execute a model.

    Args:
        engine_adapter: The engine adapter to execute queries against.
        snapshots: All upstream snapshots (by model name) to use for expansion and mapping of physical locations.
        deployability_index: Determines snapshots that are deployable in the context of this evaluation.
    """

    def __init__(
        self,
        engine_adapter: EngineAdapter,
        snapshots: t.Dict[str, Snapshot],
        deployability_index: t.Optional[DeployabilityIndex] = None,
        default_dialect: t.Optional[str] = None,
        default_catalog: t.Optional[str] = None,
        is_restatement: t.Optional[bool] = None,
        parent_intervals: t.Optional[Intervals] = None,
        variables: t.Optional[t.Dict[str, t.Any]] = None,
        blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
    ):
        self.snapshots = snapshots
        self.deployability_index = deployability_index
        self._engine_adapter = engine_adapter
        self._default_catalog = default_catalog
        self._default_dialect = default_dialect
        self._variables = variables or {}
        self._blueprint_variables = blueprint_variables or {}
        self._is_restatement = is_restatement
        self._parent_intervals = parent_intervals

    @property
    def default_dialect(self) -> t.Optional[str]:
        return self._default_dialect

    @property
    def engine_adapter(self) -> EngineAdapter:
        """Returns an engine adapter."""
        return self._engine_adapter

    @cached_property
    def _model_tables(self) -> t.Dict[str, str]:
        """Returns a mapping of model names to tables."""
        return to_table_mapping(self.snapshots.values(), self.deployability_index)

    @property
    def default_catalog(self) -> t.Optional[str]:
        return self._default_catalog

    @property
    def gateway(self) -> t.Optional[str]:
        """Returns the gateway name."""
        return self.var(c.GATEWAY)

    @property
    def is_restatement(self) -> t.Optional[bool]:
        return self._is_restatement

    @property
    def parent_intervals(self) -> t.Optional[Intervals]:
        return self._parent_intervals

    def var(self, var_name: str, default: t.Optional[t.Any] = None) -> t.Optional[t.Any]:
        """Returns a variable value."""
        return self._variables.get(var_name.lower(), default)

    def blueprint_var(self, var_name: str, default: t.Optional[t.Any] = None) -> t.Optional[t.Any]:
        """Returns a blueprint variable value."""
        return self._blueprint_variables.get(var_name.lower(), default)

    def with_variables(
        self,
        variables: t.Dict[str, t.Any],
        blueprint_variables: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> ExecutionContext:
        """Returns a new ExecutionContext with additional variables."""
        return ExecutionContext(
            self._engine_adapter,
            self.snapshots,
            self.deployability_index,
            self._default_dialect,
            self._default_catalog,
            self._is_restatement,
            variables=variables,
            blueprint_variables=blueprint_variables,
        )


class GenericContext(BaseContext, t.Generic[C]):
    """Encapsulates a SQLMesh environment supplying convenient functions to perform various tasks.

    Args:
        notification_targets: The notification target to use. Defaults to what is defined in config.
        paths: The directories containing SQLMesh files.
        config: A Config object or the name of a Config object in config.py.
        connection: The name of the connection. If not specified the first connection as it appears
            in configuration will be used.
        test_connection: The name of the connection to use for tests. If not specified the first
            connection as it appears in configuration will be used.
        concurrent_tasks: The maximum number of tasks that can use the connection concurrently.
        load: Whether or not to automatically load all models and macros (default True).
        console: The rich instance used for printing out CLI command results.
        users: A list of users to make known to SQLMesh.
    """

    CONFIG_TYPE: t.Type[C]
    """The type of config object to use (default: Config)."""

    PLAN_BUILDER_TYPE = PlanBuilder
    """The type of plan builder object to use (default: PlanBuilder)."""

    def __init__(
        self,
        notification_targets: t.Optional[t.List[NotificationTarget]] = None,
        state_sync: t.Optional[StateSync] = None,
        paths: t.Union[str | Path, t.Iterable[str | Path]] = "",
        config: t.Optional[t.Union[C, str, t.Dict[Path, C]]] = None,
        gateway: t.Optional[str] = None,
        concurrent_tasks: t.Optional[int] = None,
        loader: t.Optional[t.Type[Loader]] = None,
        load: bool = True,
        users: t.Optional[t.List[User]] = None,
        config_loader_kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        selector: t.Optional[t.Type[Selector]] = None,
    ):
        self.configs = (
            config
            if isinstance(config, dict)
            else load_configs(config, self.CONFIG_TYPE, paths, **(config_loader_kwargs or {}))
        )
        self._projects = {config.project for config in self.configs.values()}
        self.dag: DAG[str] = DAG()
        self._models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
        self._audits: UniqueKeyDict[str, ModelAudit] = UniqueKeyDict("audits")
        self._standalone_audits: UniqueKeyDict[str, StandaloneAudit] = UniqueKeyDict(
            "standaloneaudits"
        )
        self._model_test_metadata: t.List[ModelTestMetadata] = []
        self._model_test_metadata_path_index: t.Dict[Path, t.List[ModelTestMetadata]] = {}
        self._model_test_metadata_fully_qualified_name_index: t.Dict[str, ModelTestMetadata] = {}
        self._models_with_tests: t.Set[str] = set()

        self._macros: UniqueKeyDict[str, ExecutableOrMacro] = UniqueKeyDict("macros")
        self._metrics: UniqueKeyDict[str, Metric] = UniqueKeyDict("metrics")
        self._jinja_macros = JinjaMacroRegistry()
        self._requirements: t.Dict[str, str] = {}
        self._environment_statements: t.List[EnvironmentStatements] = []
        self._excluded_requirements: t.Set[str] = set()
        self._engine_adapter: t.Optional[EngineAdapter] = None
        self._linters: t.Dict[str, Linter] = {}
        self._loaded: bool = False
        self._selector_cls = selector or NativeSelector

        self.path, self.config = t.cast(t.Tuple[Path, C], next(iter(self.configs.items())))

        self._all_dialects: t.Set[str] = {self.config.dialect or ""}

        if self.config.disable_anonymized_analytics:
            analytics.disable_analytics()

        self.gateway = gateway
        self._scheduler = self.config.get_scheduler(self.gateway)
        self.environment_ttl = self.config.environment_ttl
        self.pinned_environments = Environment.sanitize_names(self.config.pinned_environments)
        self.auto_categorize_changes = self.config.plan.auto_categorize_changes
        self.selected_gateway = (gateway or self.config.default_gateway_name).lower()

        gw_model_defaults = self.config.get_gateway(self.selected_gateway).model_defaults
        if gw_model_defaults:
            # Merge global model defaults with the selected gateway's, if it's overriden
            global_defaults = self.config.model_defaults.model_dump(exclude_unset=True)
            gateway_defaults = gw_model_defaults.model_dump(exclude_unset=True)

            self.config.model_defaults = ModelDefaultsConfig(
                **{**global_defaults, **gateway_defaults}
            )

        # This allows overriding the default dialect's normalization strategy, so for example
        # one can do `dialect="duckdb,normalization_strategy=lowercase"` and this will be
        # applied to the DuckDB dialect globally
        if "normalization_strategy" in str(self.config.dialect):
            dialect = Dialect.get_or_raise(self.config.dialect)
            type(dialect).NORMALIZATION_STRATEGY = dialect.normalization_strategy

        self._loaders = [
            (loader or config.loader)(self, path, **config.loader_kwargs)
            for path, config in self.configs.items()
        ]

        self._concurrent_tasks = concurrent_tasks
        self._state_connection_config = (
            self.config.get_state_connection(self.gateway) or self.connection_config
        )

        self._snapshot_evaluator: t.Optional[SnapshotEvaluator] = None

        self.console = get_console()
        setattr(self.console, "dialect", self.config.dialect)

        self._provided_state_sync: t.Optional[StateSync] = state_sync
        self._state_sync: t.Optional[StateSync] = None

        # Should we dedupe notification_targets? If so how?
        self.notification_targets = (notification_targets or []) + self.config.notification_targets
        self.users = (users or []) + self.config.users
        self.users = list({user.username: user for user in self.users}.values())
        self._register_notification_targets()

        if load:
            self.load()

    @property
    def default_dialect(self) -> t.Optional[str]:
        return self.config.dialect

    @property
    def engine_adapter(self) -> EngineAdapter:
        """Returns the default engine adapter."""
        if self._engine_adapter is None:
            self._engine_adapter = self.connection_config.create_engine_adapter()
        return self._engine_adapter

    @property
    def snapshot_evaluator(self) -> SnapshotEvaluator:
        if not self._snapshot_evaluator:
            self._snapshot_evaluator = SnapshotEvaluator(
                {
                    gateway: adapter.with_settings(execute_log_level=logging.INFO)
                    for gateway, adapter in self.engine_adapters.items()
                },
                ddl_concurrent_tasks=self.concurrent_tasks,
                selected_gateway=self.selected_gateway,
            )
        return self._snapshot_evaluator

    def execution_context(
        self,
        deployability_index: t.Optional[DeployabilityIndex] = None,
        engine_adapter: t.Optional[EngineAdapter] = None,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
    ) -> ExecutionContext:
        """Returns an execution context."""
        return ExecutionContext(
            engine_adapter=engine_adapter or self.engine_adapter,
            snapshots=snapshots or self.snapshots,
            deployability_index=deployability_index,
            default_dialect=self.default_dialect,
            default_catalog=self.default_catalog,
        )

    @python_api_analytics
    def upsert_model(self, model: t.Union[str, Model], **kwargs: t.Any) -> Model:
        """Update or insert a model.

        The context's models dictionary will be updated to include these changes.

        Args:
            model: Model name or instance to update.
            kwargs: The kwargs to update the model with.

        Returns:
            A new instance of the updated or inserted model.
        """
        model = self.get_model(model, raise_if_missing=True)
        if not model.enabled:
            raise SQLMeshError(f"The disabled model '{model.name}' cannot be upserted")
        path = model._path

        model = model.copy(update=kwargs)
        model._path = path

        self.dag.add(model.fqn, model.depends_on)

        self._models.update(
            {
                model.fqn: model,
                # bust the fingerprint cache for all downstream models
                **{fqn: self._models[fqn].copy() for fqn in self.dag.downstream(model.fqn)},
            }
        )

        update_model_schemas(
            self.dag,
            models=self._models,
            cache_dir=self.cache_dir,
        )

        if model.dialect:
            self._all_dialects.add(model.dialect)

        model.validate_definition()

        return model

    def scheduler(
        self,
        environment: t.Optional[str] = None,
        snapshot_evaluator: t.Optional[SnapshotEvaluator] = None,
    ) -> Scheduler:
        """Returns the built-in scheduler.

        Args:
            environment: The target environment to source model snapshots from, or None
                if snapshots should be sourced from the currently loaded local state.

        Returns:
            The built-in scheduler instance.
        """
        snapshots: t.Iterable[Snapshot]
        if environment is not None:
            stored_environment = self.state_sync.get_environment(environment)
            if stored_environment is None:
                raise ConfigError(f"Environment '{environment}' was not found.")
            snapshots = self.state_sync.get_snapshots(stored_environment.snapshots).values()
        else:
            snapshots = self.snapshots.values()

        if not snapshots:
            raise ConfigError("No models were found")

        return self.create_scheduler(snapshots, snapshot_evaluator or self.snapshot_evaluator)

    def create_scheduler(
        self, snapshots: t.Iterable[Snapshot], snapshot_evaluator: SnapshotEvaluator
    ) -> Scheduler:
        """Creates the built-in scheduler.

        Args:
            snapshots: The snapshots to schedule.

        Returns:
            The built-in scheduler instance.
        """
        return Scheduler(
            snapshots,
            snapshot_evaluator,
            self.state_sync,
            default_catalog=self.default_catalog,
            max_workers=self.concurrent_tasks,
            console=self.console,
            notification_target_manager=self.notification_target_manager,
        )

    @property
    def state_sync(self) -> StateSync:
        if not self._state_sync:
            self._state_sync = self._new_state_sync()

            if self._state_sync.get_versions(validate=False).schema_version == 0:
                self.console.log_status_update("Initializing new project state...")
                self._state_sync.migrate()
            self._state_sync.get_versions()
            self._state_sync = CachingStateSync(self._state_sync)  # type: ignore
        return self._state_sync

    @property
    def state_reader(self) -> StateReader:
        return self.state_sync

    def refresh(self) -> None:
        """Refresh all models that have been updated."""
        if any(loader.reload_needed() for loader in self._loaders):
            self.load()

    def load(self, update_schemas: bool = True) -> GenericContext[C]:
        """Load all files in the context's path."""
        load_start_ts = time.perf_counter()

        loaded_projects = [loader.load() for loader in self._loaders]

        self.dag = DAG()
        self._standalone_audits.clear()
        self._audits.clear()
        self._macros.clear()
        self._models.clear()
        self._metrics.clear()
        self._requirements.clear()
        self._excluded_requirements.clear()
        self._linters.clear()
        self._environment_statements = []
        self._model_test_metadata.clear()
        self._model_test_metadata_path_index.clear()
        self._model_test_metadata_fully_qualified_name_index.clear()
        self._models_with_tests.clear()

        for loader, project in zip(self._loaders, loaded_projects):
            self._jinja_macros = self._jinja_macros.merge(project.jinja_macros)
            self._macros.update(project.macros)
            self._models.update(project.models)
            self._metrics.update(project.metrics)
            self._audits.update(project.audits)
            self._standalone_audits.update(project.standalone_audits)
            self._requirements.update(project.requirements)
            self._excluded_requirements.update(project.excluded_requirements)
            self._environment_statements.extend(project.environment_statements)

            self._model_test_metadata.extend(project.model_test_metadata)
            for metadata in project.model_test_metadata:
                if metadata.path not in self._model_test_metadata_path_index:
                    self._model_test_metadata_path_index[metadata.path] = []
                self._model_test_metadata_path_index[metadata.path].append(metadata)
                self._model_test_metadata_fully_qualified_name_index[
                    metadata.fully_qualified_test_name
                ] = metadata
                self._models_with_tests.add(metadata.model_name)

            config = loader.config
            self._linters[config.project] = Linter.from_rules(
                BUILTIN_RULES.union(project.user_rules), config.linter
            )

        # Load environment statements from state for projects not in current load
        if any(self._projects):
            prod = self.state_reader.get_environment(c.PROD)
            if prod:
                existing_statements = self.state_reader.get_environment_statements(c.PROD)
                for stmt in existing_statements:
                    if stmt.project and stmt.project not in self._projects:
                        self._environment_statements.append(stmt)

        uncached = set()

        if any(self._projects):
            prod = self.state_reader.get_environment(c.PROD)

            if prod:
                for snapshot in self.state_reader.get_snapshots(prod.snapshots).values():
                    if snapshot.node.project in self._projects:
                        uncached.add(snapshot.name)
                    else:
                        local_store = self._standalone_audits if snapshot.is_audit else self._models
                        if snapshot.name in local_store:
                            uncached.add(snapshot.name)
                        else:
                            local_store[snapshot.name] = snapshot.node  # type: ignore

        for model in self._models.values():
            self.dag.add(model.fqn, model.depends_on)

        if update_schemas:
            for fqn in self.dag:
                model = self._models.get(fqn)  # type: ignore

                if not model or fqn in uncached:
                    continue

                # make a copy of remote models that depend on local models or in the downstream chain
                # without this, a SELECT * FROM local will not propogate properly because the downstream
                # model will get mutated (schema changes) but the object is the same as the remote cache
                if any(dep in uncached for dep in model.depends_on):
                    uncached.add(fqn)
                    self._models.update({fqn: model.copy(update={"mapping_schema": {}})})
                    continue

            update_model_schemas(
                self.dag,
                models=self._models,
                cache_dir=self.cache_dir,
            )

            models = self.models.values()
            for model in models:
                # The model definition can be validated correctly only after the schema is set.
                model.validate_definition()

        duplicates = set(self._models) & set(self._standalone_audits)
        if duplicates:
            raise ConfigError(
                f"Models and Standalone audits cannot have the same name: {duplicates}"
            )

        self._all_dialects = {m.dialect for m in self._models.values() if m.dialect} | {
            self.default_dialect or ""
        }

        analytics.collector.on_project_loaded(
            project_type=self._project_type,
            models_count=len(self._models),
            audits_count=len(self._audits),
            standalone_audits_count=len(self._standalone_audits),
            macros_count=len(self._macros),
            jinja_macros_count=len(self._jinja_macros.root_macros),
            load_time_sec=time.perf_counter() - load_start_ts,
            state_sync_fingerprint=self._scheduler.state_sync_fingerprint(self),
            project_name=self.config.project,
        )

        self._loaded = True
        return self

    @python_api_analytics
    def run(
        self,
        environment: t.Optional[str] = None,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        skip_janitor: bool = False,
        ignore_cron: bool = False,
        select_models: t.Optional[t.Collection[str]] = None,
        exit_on_env_update: t.Optional[int] = None,
        no_auto_upstream: bool = False,
    ) -> CompletionStatus:
        """Run the entire dag through the scheduler.

        Args:
            environment: The target environment to source model snapshots from and virtually update. Default: prod.
            start: The start of the interval to render.
            end: The end of the interval to render.
            execution_time: The date/time time reference to use for execution time. Defaults to now.
            skip_janitor: Whether to skip the janitor task.
            ignore_cron: Whether to ignore the model's cron schedule and run all available missing intervals.
            select_models: A list of model selection expressions to filter models that should run. Note that
                upstream dependencies of selected models will also be evaluated.
            exit_on_env_update: If set, exits with the provided code if the run is interrupted by an update
                to the target environment.
            no_auto_upstream: Whether to not force upstream models to run. Only applicable when using `select_models`.

        Returns:
            True if the run was successful, False otherwise.
        """
        environment = environment or self.config.default_target_environment
        environment = Environment.sanitize_name(environment)
        if not skip_janitor and environment.lower() == c.PROD:
            self._run_janitor()

        self.notification_target_manager.notify(
            NotificationEvent.RUN_START, environment=environment
        )
        analytics_run_id = analytics.collector.on_run_start(
            engine_type=self.snapshot_evaluator.adapter.dialect,
            state_sync_type=self.state_sync.state_type(),
        )
        self._load_materializations()

        env_check_attempts_num = max(
            1,
            self.config.run.environment_check_max_wait
            // self.config.run.environment_check_interval,
        )

        def _block_until_finalized() -> str:
            for _ in range(env_check_attempts_num):
                assert environment is not None  # mypy
                environment_state = self.state_sync.get_environment(environment)
                if not environment_state:
                    raise SQLMeshError(f"Environment '{environment}' was not found.")
                if environment_state.finalized_ts:
                    return environment_state.plan_id
                self.console.log_warning(
                    f"Environment '{environment}' is being updated by plan '{environment_state.plan_id}'. "
                    f"Retrying in {self.config.run.environment_check_interval} seconds..."
                )
                time.sleep(self.config.run.environment_check_interval)
            raise SQLMeshError(
                f"Exceeded the maximum wait time for environment '{environment}' to be ready. "
                "This means that the environment either failed to update or the update is taking longer than expected. "
                "See https://sqlmesh.readthedocs.io/en/stable/reference/configuration/#run to adjust the timeout settings."
            )

        success = False
        interrupted = False
        done = False
        while not done:
            plan_id_at_start = _block_until_finalized()

            def _has_environment_changed() -> bool:
                assert environment is not None  # mypy
                current_environment_state = self.state_sync.get_environment(environment)
                return (
                    not current_environment_state
                    or current_environment_state.plan_id != plan_id_at_start
                    or not current_environment_state.finalized_ts
                )

            try:
                completion_status = self._run(
                    environment,
                    start=start,
                    end=end,
                    execution_time=execution_time,
                    ignore_cron=ignore_cron,
                    select_models=select_models,
                    circuit_breaker=_has_environment_changed,
                    no_auto_upstream=no_auto_upstream,
                )
                done = True
            except CircuitBreakerError:
                self.console.log_warning(
                    f"Environment '{environment}' modified while running. Restarting the run..."
                )
                if exit_on_env_update:
                    interrupted = True
                    done = True
            except Exception as e:
                self.notification_target_manager.notify(
                    NotificationEvent.RUN_FAILURE, traceback.format_exc()
                )
                logger.info("Run failed.", exc_info=e)
                analytics.collector.on_run_end(
                    run_id=analytics_run_id, succeeded=False, interrupted=False, error=e
                )
                raise e

        if completion_status.is_success or interrupted:
            self.notification_target_manager.notify(
                NotificationEvent.RUN_END, environment=environment
            )
            self.console.log_success(f"Run finished for environment '{environment}'")
        elif completion_status.is_failure:
            self.notification_target_manager.notify(
                NotificationEvent.RUN_FAILURE, "See console logs for details."
            )

        analytics.collector.on_run_end(
            run_id=analytics_run_id, succeeded=success, interrupted=interrupted
        )

        if interrupted and exit_on_env_update is not None:
            sys.exit(exit_on_env_update)

        return completion_status

    @python_api_analytics
    def run_janitor(self, ignore_ttl: bool) -> bool:
        success = False

        if self.console.start_cleanup(ignore_ttl):
            try:
                self._run_janitor(ignore_ttl)
                success = True
            finally:
                self.console.stop_cleanup(success=success)

        return success

    @python_api_analytics
    def destroy(self) -> bool:
        success = False

        # Collect resources to be deleted
        environments = self.state_reader.get_environments()
        schemas_to_delete = set()
        tables_to_delete = set()
        views_to_delete = set()
        all_snapshot_infos = set()

        # For each environment find schemas and tables
        for environment in environments:
            all_snapshot_infos.update(environment.snapshots)
            snapshots = self.state_reader.get_snapshots(environment.snapshots).values()
            for snapshot in snapshots:
                if snapshot.is_model and not snapshot.is_symbolic:
                    # Get the appropriate adapter
                    if environment.gateway_managed and snapshot.model_gateway:
                        adapter = self.engine_adapters.get(
                            snapshot.model_gateway, self.engine_adapter
                        )
                    else:
                        adapter = self.engine_adapter

                    if environment.suffix_target.is_schema or environment.suffix_target.is_catalog:
                        schema = snapshot.qualified_view_name.schema_for_environment(
                            environment.naming_info, dialect=adapter.dialect
                        )
                        catalog = snapshot.qualified_view_name.catalog_for_environment(
                            environment.naming_info, dialect=adapter.dialect
                        )
                        if catalog:
                            schemas_to_delete.add(f"{catalog}.{schema}")
                        else:
                            schemas_to_delete.add(schema)

                    if environment.suffix_target.is_table:
                        view_name = snapshot.qualified_view_name.for_environment(
                            environment.naming_info, dialect=adapter.dialect
                        )
                        views_to_delete.add(view_name)

                    # Add snapshot tables
                    table_name = snapshot.table_name()
                    tables_to_delete.add(table_name)

        if self.console.start_destroy(schemas_to_delete, views_to_delete, tables_to_delete):
            try:
                success = self._destroy()
            finally:
                self.console.stop_destroy(success=success)

        return success

    @t.overload
    def get_model(
        self, model_or_snapshot: ModelOrSnapshot, raise_if_missing: Literal[True] = True
    ) -> Model: ...

    @t.overload
    def get_model(
        self,
        model_or_snapshot: ModelOrSnapshot,
        raise_if_missing: Literal[False] = False,
    ) -> t.Optional[Model]: ...

    def get_model(
        self, model_or_snapshot: ModelOrSnapshot, raise_if_missing: bool = False
    ) -> t.Optional[Model]:
        """Returns a model with the given name or None if a model with such name doesn't exist.

        Args:
            model_or_snapshot: A model name, model, or snapshot.
            raise_if_missing: Raises an error if a model is not found.

        Returns:
            The expected model.
        """
        if isinstance(model_or_snapshot, Snapshot):
            return model_or_snapshot.model
        if not isinstance(model_or_snapshot, str):
            return model_or_snapshot

        try:
            # We should try all dialects referenced in the project for cases when models use mixed dialects.
            for dialect in self._all_dialects:
                normalized_name = normalize_model_name(
                    model_or_snapshot,
                    dialect=dialect,
                    default_catalog=self.default_catalog,
                )
                if normalized_name in self._models:
                    return self._models[normalized_name]
        except:
            pass

        if raise_if_missing:
            if model_or_snapshot.endswith((".sql", ".py")):
                msg = "Resolving models by path is not supported, please pass in the model name instead."
            else:
                msg = f"Cannot find model with name '{model_or_snapshot}'"

            raise SQLMeshError(msg)

        return None

    @t.overload
    def get_snapshot(self, node_or_snapshot: NodeOrSnapshot) -> t.Optional[Snapshot]: ...

    @t.overload
    def get_snapshot(
        self, node_or_snapshot: NodeOrSnapshot, raise_if_missing: Literal[True]
    ) -> Snapshot: ...

    @t.overload
    def get_snapshot(
        self, node_or_snapshot: NodeOrSnapshot, raise_if_missing: Literal[False]
    ) -> t.Optional[Snapshot]: ...

    def get_snapshot(
        self, node_or_snapshot: NodeOrSnapshot, raise_if_missing: bool = False
    ) -> t.Optional[Snapshot]:
        """Returns a snapshot with the given name or None if a snapshot with such name doesn't exist.

        Args:
            node_or_snapshot: A node name, node, or snapshot.
            raise_if_missing: Raises an error if a snapshot is not found.

        Returns:
            The expected snapshot.
        """
        if isinstance(node_or_snapshot, Snapshot):
            return node_or_snapshot
        fqn = self._node_or_snapshot_to_fqn(node_or_snapshot)
        snapshot = self.snapshots.get(fqn)

        if raise_if_missing and not snapshot:
            raise SQLMeshError(f"Cannot find snapshot for '{fqn}'")

        return snapshot

    def config_for_path(self, path: Path) -> t.Tuple[Config, Path]:
        """Returns the config and path of the said project for a given file path."""
        for config_path, config in self.configs.items():
            try:
                path.relative_to(config_path)
                return config, config_path
            except ValueError:
                pass
        return self.config, self.path

    def config_for_node(self, node: Model | Audit) -> Config:
        path = node._path
        if path is None:
            return self.config
        return self.config_for_path(path)[0]  # type: ignore

    @property
    def models(self) -> MappingProxyType[str, Model]:
        """Returns all registered models in this context."""
        return MappingProxyType(self._models)

    @property
    def metrics(self) -> MappingProxyType[str, Metric]:
        """Returns all registered metrics in this context."""
        return MappingProxyType(self._metrics)

    @property
    def standalone_audits(self) -> MappingProxyType[str, StandaloneAudit]:
        """Returns all registered standalone audits in this context."""
        return MappingProxyType(self._standalone_audits)

    @property
    def models_with_tests(self) -> t.Set[str]:
        """Returns all models with tests in this context."""
        return self._models_with_tests

    @property
    def snapshots(self) -> t.Dict[str, Snapshot]:
        """Generates and returns snapshots based on models registered in this context.

        If one of the snapshots has been previously stored in the persisted state, the stored
        instance will be returned.
        """
        return self._snapshots()

    @property
    def requirements(self) -> t.Dict[str, str]:
        """Returns the Python dependencies of the project loaded in this context."""
        return self._requirements.copy()

    @cached_property
    def default_catalog(self) -> t.Optional[str]:
        return self.default_catalog_per_gateway.get(self.selected_gateway)

    @python_api_analytics
    def render(
        self,
        model_or_snapshot: ModelOrSnapshot,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        expand: t.Union[bool, t.Iterable[str]] = False,
        **kwargs: t.Any,
    ) -> exp.Expr:
        """Renders a model's query, expanding macros with provided kwargs, and optionally expanding referenced models.

        Args:
            model_or_snapshot: The model, model name, or snapshot to render.
            start: The start of the interval to render.
            end: The end of the interval to render.
            execution_time: The date/time time reference to use for execution time. Defaults to now.
            expand: Whether or not to use expand materialized models, defaults to False.
                If True, all referenced models are expanded as raw queries.
                If a list, only referenced models are expanded as raw queries.

        Returns:
            The rendered expression.
        """
        execution_time = execution_time or now()

        model = self.get_model(model_or_snapshot, raise_if_missing=True)

        if expand and not isinstance(expand, bool):
            expand = {
                normalize_model_name(
                    x, default_catalog=self.default_catalog, dialect=self.default_dialect
                )
                for x in expand
            }

        expand = self.dag.upstream(model.fqn) if expand is True else expand or []

        if model.is_seed:
            import pandas as pd

            df = next(
                model.render(
                    context=self.execution_context(
                        engine_adapter=self._get_engine_adapter(model.gateway)
                    ),
                    start=start,
                    end=end,
                    execution_time=execution_time,
                    **kwargs,
                )
            )
            return next(pandas_to_sql(t.cast(pd.DataFrame, df), model.columns_to_types))

        snapshots = self.snapshots
        deployability_index = DeployabilityIndex.create(snapshots.values(), start=start)

        return model.render_query_or_raise(
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=snapshots,
            expand=expand,
            deployability_index=deployability_index,
            engine_adapter=self._get_engine_adapter(model.gateway),
            **kwargs,
        )

    @python_api_analytics
    def evaluate(
        self,
        model_or_snapshot: ModelOrSnapshot,
        start: TimeLike,
        end: TimeLike,
        execution_time: TimeLike,
        limit: t.Optional[int] = None,
        **kwargs: t.Any,
    ) -> DF:
        """Evaluate a model or snapshot (running its query against a DB/Engine).

        This method is used to test or iterate on models without side effects.

        Args:
            model_or_snapshot: The model, model name, or snapshot to render.
            start: The start of the interval to evaluate.
            end: The end of the interval to evaluate.
            execution_time: The date/time time reference to use for execution time.
            limit: A limit applied to the model.
        """
        snapshots = self.snapshots
        fqn = self._node_or_snapshot_to_fqn(model_or_snapshot)
        if fqn not in snapshots:
            raise SQLMeshError(f"Cannot find snapshot for '{fqn}'")
        snapshot = snapshots[fqn]

        # Expand all uncategorized parents since physical tables don't exist for them yet
        expand = [
            parent
            for parent in self.dag.upstream(snapshot.model.fqn)
            if (parent_snapshot := snapshots.get(parent))
            and parent_snapshot.is_model
            and parent_snapshot.model.is_sql
            and not parent_snapshot.categorized
        ]

        df = self.snapshot_evaluator.evaluate_and_fetch(
            snapshot,
            start=start,
            end=end,
            execution_time=execution_time,
            snapshots=self.snapshots,
            limit=limit or c.DEFAULT_MAX_LIMIT,
            expand=expand,
        )

        if df is None:
            raise RuntimeError(f"Error evaluating {snapshot.name}")

        return df

    @python_api_analytics
    def format(
        self,
        transpile: t.Optional[str] = None,
        rewrite_casts: t.Optional[bool] = None,
        append_newline: t.Optional[bool] = None,
        *,
        check: t.Optional[bool] = None,
        paths: t.Optional[t.Tuple[t.Union[str, Path], ...]] = None,
        **kwargs: t.Any,
    ) -> bool:
        """Format all SQL models and audits."""
        filtered_targets = [
            target
            for target in chain(self._models.values(), self._audits.values())
            if target._path is not None
            and target._path.suffix == ".sql"
            and (not paths or any(target._path.samefile(p) for p in paths))
        ]
        unformatted_file_paths = []

        for target in filtered_targets:
            if (
                target._path is None or target.formatting is False
            ):  # introduced to satisfy type checker as still want to pull filter out as many targets as possible before loop
                continue

            with open(target._path, "r+", encoding="utf-8") as file:
                before = file.read()

                after = self._format(
                    target,
                    before,
                    transpile=transpile,
                    rewrite_casts=rewrite_casts,
                    append_newline=append_newline,
                    **kwargs,
                )

                if not check:
                    file.seek(0)
                    file.write(after)
                    file.truncate()
                elif before != after:
                    unformatted_file_paths.append(target._path)

        if unformatted_file_paths:
            for path in unformatted_file_paths:
                self.console.log_status_update(f"{path} needs reformatting.")
            self.console.log_status_update(
                f"\n{len(unformatted_file_paths)} file(s) need reformatting."
            )
            return False

        return True

    def _format(
        self,
        target: Model | Audit,
        before: str,
        *,
        transpile: t.Optional[str] = None,
        rewrite_casts: t.Optional[bool] = None,
        append_newline: t.Optional[bool] = None,
        **kwargs: t.Any,
    ) -> str:
        expressions = parse(before, default_dialect=self.config_for_node(target).dialect)
        if transpile and is_meta_expression(expressions[0]):
            for prop in expressions[0].expressions:
                if prop.name.lower() == "dialect":
                    prop.replace(
                        exp.Property(
                            this="dialect",
                            value=exp.Literal.string(transpile or target.dialect),
                        )
                    )

        format_config = self.config_for_node(target).format
        after = format_model_expressions(
            expressions,
            transpile or target.dialect,
            rewrite_casts=(
                rewrite_casts if rewrite_casts is not None else not format_config.no_rewrite_casts
            ),
            **{**format_config.generator_options, **kwargs},
        )

        if append_newline is None:
            append_newline = format_config.append_newline
        if append_newline:
            after += "\n"

        return after

    @python_api_analytics
    def plan(
        self,
        environment: t.Optional[str] = None,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        create_from: t.Optional[str] = None,
        skip_tests: t.Optional[bool] = None,
        restate_models: t.Optional[t.Iterable[str]] = None,
        no_gaps: t.Optional[bool] = None,
        skip_backfill: t.Optional[bool] = None,
        empty_backfill: t.Optional[bool] = None,
        forward_only: t.Optional[bool] = None,
        allow_destructive_models: t.Optional[t.Collection[str]] = None,
        allow_additive_models: t.Optional[t.Collection[str]] = None,
        no_prompts: t.Optional[bool] = None,
        auto_apply: t.Optional[bool] = None,
        no_auto_categorization: t.Optional[bool] = None,
        effective_from: t.Optional[TimeLike] = None,
        include_unmodified: t.Optional[bool] = None,
        select_models: t.Optional[t.Collection[str]] = None,
        backfill_models: t.Optional[t.Collection[str]] = None,
        categorizer_config: t.Optional[CategorizerConfig] = None,
        enable_preview: t.Optional[bool] = None,
        no_diff: t.Optional[bool] = None,
        run: t.Optional[bool] = None,
        diff_rendered: t.Optional[bool] = None,
        skip_linter: t.Optional[bool] = None,
        explain: t.Optional[bool] = None,
        ignore_cron: t.Optional[bool] = None,
        min_intervals: t.Optional[int] = None,
    ) -> Plan:
        """Interactively creates a plan.

        This method compares the current context with the target environment. It then presents
        the differences and asks whether to backfill each modified model.

        Args:
            environment: The environment to diff and plan against.
            start: The start date of the backfill if there is one.
            end: The end date of the backfill if there is one.
            execution_time: The date/time reference to use for execution time. Defaults to now.
            create_from: The environment to create the target environment from if it
                doesn't exist. If not specified, the "prod" environment will be used.
            skip_tests: Unit tests are run by default so this will skip them if enabled
            restate_models: A list of either internal or external models, or tags, that need to be restated
                for the given plan interval. If the target environment is a production environment,
                ALL snapshots that depended on these upstream tables will have their intervals deleted
                (even ones not in this current environment). Only the snapshots in this environment will
                be backfilled whereas others need to be recovered on a future plan application. For development
                environments only snapshots that are part of this plan will be affected.
            no_gaps:  Whether to ensure that new snapshots for models that are already a
                part of the target environment have no data gaps when compared against previous
                snapshots for same models.
            skip_backfill: Whether to skip the backfill step. Default: False.
            empty_backfill: Like skip_backfill, but also records processed intervals.
            forward_only: Whether the purpose of the plan is to make forward only changes.
            allow_destructive_models: Models whose forward-only changes are allowed to be destructive.
            allow_additive_models: Models whose forward-only changes are allowed to be additive.
            no_prompts: Whether to disable interactive prompts for the backfill time range. Please note that
                if this flag is set to true and there are uncategorized changes the plan creation will
                fail. Default: False.
            auto_apply: Whether to automatically apply the new plan after creation. Default: False.
            no_auto_categorization: Indicates whether to disable automatic categorization of model
                changes (breaking / non-breaking). If not provided, then the corresponding configuration
                option determines the behavior.
            categorizer_config: The configuration for the categorizer. Uses the categorizer configuration defined in the
                project config by default.
            effective_from: The effective date from which to apply forward-only changes on production.
            include_unmodified: Indicates whether to include unmodified models in the target development environment.
            select_models: A list of model selection strings to filter the models that should be included into this plan.
            backfill_models: A list of model selection strings to filter the models for which the data should be backfilled.
            enable_preview: Indicates whether to enable preview for forward-only models in development environments.
            no_diff: Hide text differences for changed models.
            run: Whether to run latest intervals as part of the plan application.
            diff_rendered: Whether the diff should compare raw vs rendered models
            skip_linter: Linter runs by default so this will skip it if enabled
            explain: Whether to explain the plan instead of applying it.
            min_intervals: Adjust the plan start date on a per-model basis in order to ensure at least this many intervals are covered
                on every model when checking for missing intervals

        Returns:
            The populated Plan object.
        """
        plan_builder = self.plan_builder(
            environment,
            start=start,
            end=end,
            execution_time=execution_time,
            create_from=create_from,
            skip_tests=skip_tests,
            restate_models=restate_models,
            no_gaps=no_gaps,
            skip_backfill=skip_backfill,
            empty_backfill=empty_backfill,
            forward_only=forward_only,
            allow_destructive_models=allow_destructive_models,
            allow_additive_models=allow_additive_models,
            no_auto_categorization=no_auto_categorization,
            effective_from=effective_from,
            include_unmodified=include_unmodified,
            select_models=select_models,
            backfill_models=backfill_models,
            categorizer_config=categorizer_config,
            enable_preview=enable_preview,
            run=run,
            diff_rendered=diff_rendered,
            skip_linter=skip_linter,
            explain=explain,
            ignore_cron=ignore_cron,
            min_intervals=min_intervals,
        )

        plan = plan_builder.build()

        if no_auto_categorization or plan.uncategorized:
            # Prompts are required if the auto categorization is disabled
            # or if there are any uncategorized snapshots in the plan
            no_prompts = False

        if explain:
            auto_apply = True

        self.console.plan(
            plan_builder,
            auto_apply if auto_apply is not None else self.config.plan.auto_apply,
            self.default_catalog,
            no_diff=no_diff if no_diff is not None else self.config.plan.no_diff,
            no_prompts=no_prompts if no_prompts is not None else self.config.plan.no_prompts,
        )

        return plan

    @python_api_analytics
    def plan_builder(
        self,
        environment: t.Optional[str] = None,
        *,
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        execution_time: t.Optional[TimeLike] = None,
        create_from: t.Optional[str] = None,
        skip_tests: t.Optional[bool] = None,
        restate_models: t.Optional[t.Iterable[str]] = None,
        no_gaps: t.Optional[bool] = None,
        skip_backfill: t.Optional[bool] = None,
        empty_backfill: t.Optional[bool] = None,
        forward_only: t.Optional[bool] = None,
        allow_destructive_models: t.Optional[t.Collection[str]] = None,
        allow_additive_models: t.Optional[t.Collection[str]] = None,
        no_auto_categorization: t.Optional[bool] = None,
        effective_from: t.Optional[TimeLike] = None,
        include_unmodified: t.Optional[bool] = None,
        select_models: t.Optional[t.Collection[str]] = None,
        backfill_models: t.Optional[t.Collection[str]] = None,
        categorizer_config: t.Optional[CategorizerConfig] = None,
        enable_preview: t.Optional[bool] = None,
        run: t.Optional[bool] = None,
        diff_rendered: t.Optional[bool] = None,
        skip_linter: t.Optional[bool] = None,
        explain: t.Optional[bool] = None,
        ignore_cron: t.Optional[bool] = None,
        min_intervals: t.Optional[int] = None,
        always_include_local_changes: t.Optional[bool] = None,
    ) -> PlanBuilder:
        """Creates a plan builder.

        Args:
            environment: The environment to diff and plan against.
            start: The start date of the backfill if there is one.
            end: The end date of the backfill if there is one.
            execution_time: The date/time reference to use for execution time. Defaults to now.
            create_from: The environment to create the target environment from if it
                doesn't exist. If not specified, the "prod" environment will be used.
            skip_tests: Unit tests are run by default so this will skip them if enabled
            restate_models: A list of either internal or external models, or tags, that need to be restated
                for the given plan interval. If the target environment is a production environment,
                ALL snapshots that depended on these upstream tables will have their intervals deleted
                (even ones not in this current environment). Only the snapshots in this environment will
                be backfilled whereas others need to be recovered on a future plan application. For development
                environments only snapshots that are part of this plan will be affected.
            no_gaps:  Whether to ensure that new snapshots for models that are already a
                part of the target environment have no data gaps when compared against previous
                snapshots for same models.
            skip_backfill: Whether to skip the backfill step. Default: False.
            empty_backfill: Like skip_backfill, but also records processed intervals.
            forward_only: Whether the purpose of the plan is to make forward only changes.
            allow_destructive_models: Models whose forward-only changes are allowed to be destructive.
            no_auto_categorization: Indicates whether to disable automatic categorization of model
                changes (breaking / non-breaking). If not provided, then the corresponding configuration
                option determines the behavior.
            categorizer_config: The configuration for the categorizer. Uses the categorizer configuration defined in the
                project config by default.
            effective_from: The effective date from which to apply forward-only changes on production.
            include_unmodified: Indicates whether to include unmodified models in the target development environment.
            select_models: A list of model selection strings to filter the models that should be included into this plan.
            backfill_models: A list of model selection strings to filter the models for which the data should be backfilled.
            enable_preview: Indicates whether to enable preview for forward-only models in development environments.
            run: Whether to run latest intervals as part of the plan application.
            diff_rendered: Whether the diff should compare raw vs rendered models
            min_intervals: Adjust the plan start date on a per-model basis in order to ensure at least this many intervals are covered
                on every model when checking for missing intervals
            always_include_local_changes: Usually when restatements are present, local changes in the filesystem are ignored.
                However, it can be desirable to deploy changes + restatements in the same plan, so this flag overrides the default behaviour.

        Returns:
            The plan builder.
        """
        kwargs: t.Dict[str, t.Optional[UserProvidedFlags]] = {
            "start": start,
            "end": end,
            "execution_time": execution_time,
            "create_from": create_from,
            "skip_tests": skip_tests,
            "restate_models": list(restate_models) if restate_models is not None else None,
            "no_gaps": no_gaps,
            "skip_backfill": skip_backfill,
            "empty_backfill": empty_backfill,
            "forward_only": forward_only,
            "allow_destructive_models": list(allow_destructive_models)
            if allow_destructive_models is not None
            else None,
            "allow_additive_models": list(allow_additive_models)
            if allow_additive_models is not None
            else None,
            "no_auto_categorization": no_auto_categorization,
            "effective_from": effective_from,
            "include_unmodified": include_unmodified,
            "select_models": list(select_models) if select_models is not None else None,
            "backfill_models": list(backfill_models) if backfill_models is not None else None,
            "enable_preview": enable_preview,
            "run": run,
            "diff_rendered": diff_rendered,
            "skip_linter": skip_linter,
            "min_intervals": min_intervals,
        }
        user_provided_flags: t.Dict[str, UserProvidedFlags] = {
            k: v for k, v in kwargs.items() if v is not None
        }

        skip_tests = explain or skip_tests or False
        no_gaps = no_gaps or False
        skip_backfill = skip_backfill or False
        empty_backfill = empty_backfill or False
        run = run or False
        diff_rendered = diff_rendered or False
        skip_linter = skip_linter or False
        min_intervals = min_intervals or 0

        environment = environment or self.config.default_target_environment
        environment = Environment.sanitize_name(environment)
        is_dev = environment != c.PROD

        if include_unmodified is None:
            include_unmodified = self.config.plan.include_unmodified

        if skip_backfill and not no_gaps and not is_dev:
            # note: we deliberately don't mention the --no-gaps flag in case the plan came from the sqlmesh_dbt command
            # todo: perhaps we could have better error messages if we check sys.argv[0] for which cli is running?
            self.console.log_warning(
                "Skipping the backfill stage for production can lead to unexpected results, such as tables being empty or incremental data with non-contiguous time ranges being made available.\n"
                "If you are doing this deliberately to create an empty version of a table to test a change, please consider using Virtual Data Environments instead."
            )

        if not skip_linter:
            self.lint_models()

        self._run_plan_tests(skip_tests=skip_tests)

        environment_ttl = (
            self.environment_ttl if environment not in self.pinned_environments else None
        )

        model_selector = self._new_selector()

        if allow_destructive_models:
            expanded_destructive_models = model_selector.expand_model_selections(
                allow_destructive_models
            )
        else:
            expanded_destructive_models = None

        if allow_additive_models:
            expanded_additive_models = model_selector.expand_model_selections(allow_additive_models)
        else:
            expanded_additive_models = None

        if backfill_models:
            backfill_models = model_selector.expand_model_selections(backfill_models)
        else:
            backfill_models = None

        models_override: t.Optional[UniqueKeyDict[str, Model]] = None
        selected_fqns: t.Set[str] = set()
        selected_deletion_fqns: t.Set[str] = set()
        if select_models:
            try:
                models_override, selected_fqns = model_selector.select_models(
                    select_models,
                    environment,
                    fallback_env_name=create_from or c.PROD,
                    ensure_finalized_snapshots=self.config.plan.use_finalized_state,
                )
            except SQLMeshError as e:
                logger.exception(e)  # ensure the full stack trace is logged
                raise PlanError(
                    f"{e}\nCheck the SQLMesh log file for the full stack trace.\nIf the model has been fixed locally, please ensure that the --select-model expression includes it."
                )
            if not backfill_models:
                # Only backfill selected models unless explicitly specified.
                backfill_models = model_selector.expand_model_selections(select_models)

            if not backfill_models:
                # The selection matched nothing locally. Check whether it matched models
                # in the deployed environment that were deleted locally.
                selected_deletion_fqns = selected_fqns - set(self._models)

        expanded_restate_models = None
        if restate_models is not None:
            expanded_restate_models = model_selector.expand_model_selections(restate_models)

        if (restate_models is not None and not expanded_restate_models) or (
            backfill_models is not None and not backfill_models and not selected_deletion_fqns
        ):
            raise PlanError(
                "Selector did not return any models. Please check your model selection and try again."
            )

        if always_include_local_changes is None:
            # default behaviour - if restatements are detected; we operate entirely out of state and ignore local changes
            force_no_diff = restate_models is not None or (
                backfill_models is not None and not backfill_models and not selected_deletion_fqns
            )
        else:
            force_no_diff = not always_include_local_changes

        snapshots = self._snapshots(models_override)
        context_diff = self._context_diff(
            environment or c.PROD,
            snapshots=snapshots,
            create_from=create_from,
            force_no_diff=force_no_diff,
            ensure_finalized_snapshots=self.config.plan.use_finalized_state,
            diff_rendered=diff_rendered,
            always_recreate_environment=self.config.plan.always_recreate_environment,
        )
        modified_model_names = {
            *context_diff.modified_snapshots,
            *[s.name for s in context_diff.added],
        }

        if (
            is_dev
            and not include_unmodified
            and backfill_models is None
            and expanded_restate_models is None
        ):
            # Only backfill modified and added models.
            # This ensures that no models outside the impacted sub-DAG(s) will be backfilled unexpectedly.
            backfill_models = modified_model_names or None

        max_interval_end_per_model = None
        default_start, default_end = None, None
        if not run:
            ignore_cron = False
            max_interval_end_per_model = self._get_max_interval_end_per_model(
                snapshots, backfill_models
            )
            # If no end date is specified, use the max interval end from prod
            # to prevent unintended evaluation of the entire DAG.
            default_start, default_end = self._get_plan_default_start_end(
                snapshots,
                max_interval_end_per_model,
                backfill_models,
                modified_model_names,
                execution_time or now(),
            )

            # Refresh snapshot intervals to ensure that they are up to date with values reflected in the max_interval_end_per_model.
            self.state_sync.refresh_snapshot_intervals(context_diff.snapshots.values())

        start_override_per_model = self._calculate_start_override_per_model(
            min_intervals,
            start or default_start,
            end or default_end,
            execution_time or now(),
            backfill_models,
            snapshots,
            max_interval_end_per_model,
        )

        if not self.config.virtual_environment_mode.is_full:
            forward_only = True
        elif forward_only is None:
            forward_only = self.config.plan.forward_only

        # When handling prod restatements, only clear intervals from other model versions if we are using full virtual environments
        # If we are not, then there is no point, because none of the data in dev environments can be promoted by definition
        restate_all_snapshots = (
            expanded_restate_models is not None
            and not is_dev
            and self.config.virtual_environment_mode.is_full
        )

        return self.PLAN_BUILDER_TYPE(
            context_diff=context_diff,
            start=start,
            end=end,
            execution_time=execution_time,
            apply=self.apply,
            restate_models=expanded_restate_models,
            restate_all_snapshots=restate_all_snapshots,
            backfill_models=backfill_models,
            no_gaps=no_gaps,
            skip_backfill=skip_backfill,
            empty_backfill=empty_backfill,
            is_dev=is_dev,
            forward_only=forward_only,
            allow_destructive_models=expanded_destructive_models,
            allow_additive_models=expanded_additive_models,
            environment_ttl=environment_ttl,
            environment_suffix_target=self.config.environment_suffix_target,
            environment_catalog_mapping=self.environment_catalog_mapping,
            categorizer_config=categorizer_config or self.auto_categorize_changes,
            auto_categorization_enabled=not no_auto_categorization,
            effective_from=effective_from,
            include_unmodified=include_unmodified,
            default_start=default_start,
            default_end=default_end,
            enable_preview=(
                enable_preview if enable_preview is not None else self._plan_preview_enabled
            ),
            end_bounded=not run,
            ensure_finalized_snapshots=self.config.plan.use_finalized_state,
            start_override_per_model=start_override_per_model,
            end_override_per_model=max_interval_end_per_model,
            console=self.console,
            user_provided_flags=user_provided_flags,
            selected_models={
                dbt_unique_id
                for model in model_selector.expand_model_selections(select_models or "*")
                if (dbt_unique_id := snapshots[model].node.dbt_unique_id)
            },
            explain=explain or False,
            ignore_cron=ignore_cron or False,
        )

    def apply(
        self,
        plan: Plan,
        circuit_breaker: t.Optional[t.Callable[[], bool]] = None,
    ) -> None:
        """Applies a plan by pushing snapshots and backfilling data.

        Given a plan, it pushes snapshots into the state sync and then uses the scheduler
        to backfill all models.

        Args:
            plan: The plan to apply.
            circuit_breaker: An optional handler which checks if the apply should be aborted.
        """
        if (
            not plan.context_diff.has_changes
            and not plan.requires_backfill
            and not plan.has_unmodified_unpromoted
        ):
            return
        if plan.uncategorized:
            raise UncategorizedPlanError("Can't apply a plan with uncategorized changes.")

        if plan.explain:
            explainer = PlanExplainer(
                state_reader=self.state_reader,
                default_catalog=self.default_catalog,
                console=self.console,
            )
            explainer.evaluate(plan.to_evaluatable())
            return

        self.notification_target_manager.notify(
            NotificationEvent.APPLY_START,
            environment=plan.environment_naming_info.name,
            plan_id=plan.plan_id,
        )
        try:
            self._apply(plan, circuit_breaker)
        except Exception as e:
            self.notification_target_manager.notify(
                NotificationEvent.APPLY_FAILURE,
                environment=plan.environment_naming_info.name,
                plan_id=plan.plan_id,
                exc=traceback.format_exc(),
            )
            logger.info("Plan application failed.", exc_info=e)
            raise e
        self.notification_target_manager.notify(
            NotificationEvent.APPLY_END,
            environment=plan.environment_naming_info.name,
            plan_id=plan.plan_id,
        )

    @python_api_analytics
    def invalidate_environment(self, name: str, sync: bool = False) -> None:
        """Invalidates the target environment by setting its expiration timestamp to now.

        Args:
            name: The name of the environment to invalidate.
            sync: If True, the call blocks until the environment is deleted. Otherwise, the environment will
                be deleted asynchronously by the janitor process.
        """
        name = Environment.sanitize_name(name)
        self.state_sync.invalidate_environment(name)
        if sync:
            self._cleanup_environments()
            self.console.log_success(f"Environment '{name}' deleted.")
        else:
            self.console.log_success(f"Environment '{name}' invalidated.")

    @python_api_analytics
    def diff(self, environment: t.Optional[str] = None, detailed: bool = False) -> bool:
        """Show a diff of the current context with a given environment.

        Args:
            environment: The environment to diff against.
            detailed: Show the actual SQL differences if True.

        Returns:
            True if there are changes, False otherwise.
        """
        environment = environment or self.config.default_target_environment
        environment = Environment.sanitize_name(environment)
        context_diff = self._context_diff(environment)
        self.console.show_environment_difference_summary(
            context_diff,
            no_diff=not detailed,
        )
        if context_diff.has_changes:
            self.console.show_model_difference_summary(
                context_diff,
                EnvironmentNamingInfo.from_environment_catalog_mapping(
                    self.environment_catalog_mapping,
                    name=environment,
                    suffix_target=self.config.environment_suffix_target,
                    normalize_name=context_diff.normalize_environment_name,
                ),
                self.default_catalog,
                no_diff=not detailed,
            )
        return context_diff.has_changes

    @python_api_analytics
    def table_diff(
        self,
        source: str,
        target: str,
        on: t.Optional[t.List[str] | exp.Expr] = None,
        skip_columns: t.Optional[t.List[str]] = None,
        select_models: t.Optional[t.Collection[str]] = None,
        where: t.Optional[str | exp.Expr] = None,
        limit: int = 20,
        show: bool = True,
        show_sample: bool = True,
        decimals: int = 3,
        skip_grain_check: bool = False,
        warn_grain_check: bool = False,
        temp_schema: t.Optional[str] = None,
        schema_diff_ignore_case: bool = False,
        **kwargs: t.Any,  # catch-all to prevent an 'unexpected keyword argument' error if an table_diff extension passes in some extra arguments
    ) -> t.List[TableDiff]:
        """Show a diff between two tables.

        Args:
            source: The source environment or table.
            target: The target environment or table.
            on: The join condition, table aliases must be "s" and "t" for source and target.
                If omitted, the table's grain will be used.
            skip_columns: The columns to skip when computing the table diff.
            select_models: The models or snapshots to use when environments are passed in.
            where: An optional where statement to filter results.
            limit: The limit of the sample dataframe.
            show: Show the table diff output in the console.
            show_sample: Show the sample dataframe in the console. Requires show=True.
            decimals: The number of decimal places to keep when comparing floating point columns.
            skip_grain_check: Skip check for rows that contain null or duplicate grains.
            temp_schema: The schema to use for temporary tables.

        Returns:
            The list of TableDiff objects containing schema and summary differences.
        """

        if "|" in source or "|" in target:
            raise ConfigError(
                "Cross-database table diffing is available in Tobiko Cloud. Read more here: "
                "https://sqlmesh.readthedocs.io/en/stable/guides/tablediff/#diffing-tables-or-views-across-gateways"
            )

        table_diffs: t.List[TableDiff] = []

        # Diffs multiple or a single model across two environments
        if select_models:
            source_env = self.state_reader.get_environment(source)
            target_env = self.state_reader.get_environment(target)
            if not source_env:
                raise SQLMeshError(f"Could not find environment '{source}'")
            if not target_env:
                raise SQLMeshError(f"Could not find environment '{target}'")
            criteria = ", ".join(f"'{c}'" for c in select_models)
            try:
                selected_models = self._new_selector().expand_model_selections(select_models)
                if not selected_models:
                    self.console.log_status_update(
                        f"No models matched the selection criteria: {criteria}"
                    )
            except Exception as e:
                raise SQLMeshError(e)

            models_to_diff: t.List[
                t.Tuple[Model, EngineAdapter, str, str, t.Optional[t.List[str] | exp.Expr]]
            ] = []
            models_without_grain: t.List[Model] = []
            source_snapshots_to_name = {
                snapshot.name: snapshot for snapshot in source_env.snapshots
            }
            target_snapshots_to_name = {
                snapshot.name: snapshot for snapshot in target_env.snapshots
            }

            for model_fqn in selected_models:
                model = self._models[model_fqn]
                adapter = self._get_engine_adapter(model.gateway)
                source_snapshot = source_snapshots_to_name.get(model.fqn)
                target_snapshot = target_snapshots_to_name.get(model.fqn)

                if target_snapshot and source_snapshot:
                    if (source_snapshot.fingerprint != target_snapshot.fingerprint) and (
                        (source_snapshot.version != target_snapshot.version)
                        or source_snapshot.is_forward_only
                    ):
                        # Compare the virtual layer instead of the physical layer because the virtual layer is guaranteed to point
                        # to the correct/active snapshot for the model in the specified environment, taking into account things like dev previews
                        source = source_snapshot.qualified_view_name.for_environment(
                            source_env.naming_info, adapter.dialect
                        )
                        target = target_snapshot.qualified_view_name.for_environment(
                            target_env.naming_info, adapter.dialect
                        )
                        model_on = on or model.on
                        if not model_on:
                            models_without_grain.append(model)
                        else:
                            models_to_diff.append((model, adapter, source, target, model_on))

            if models_without_grain:
                model_names = "\n".join(
                    f"─ {model.name} \n  at '{model._path}'" for model in models_without_grain
                )
                message = (
                    "SQLMesh doesn't know how to join the tables for the following models:\n"
                    f"{model_names}\n\n"
                    "Please specify a `grain` in each model definition. It must be unique and not null."
                )
                if warn_grain_check:
                    self.console.log_warning(message)
                else:
                    raise SQLMeshError(message)

            if models_to_diff:
                self.console.show_table_diff_details(
                    [model[0].name for model in models_to_diff],
                )

                self.console.start_table_diff_progress(len(models_to_diff))
                try:
                    tasks_num = min(len(models_to_diff), self.concurrent_tasks)
                    table_diffs = concurrent_apply_to_values(
                        list(models_to_diff),
                        lambda model_info: self._model_diff(
                            model=model_info[0],
                            adapter=model_info[1],
                            source=model_info[2],
                            target=model_info[3],
                            on=model_info[4],
                            source_alias=source_env.name,
                            target_alias=target_env.name,
                            limit=limit,
                            decimals=decimals,
                            skip_columns=skip_columns,
                            where=where,
                            show=show,
                            temp_schema=temp_schema,
                            skip_grain_check=skip_grain_check,
                            schema_diff_ignore_case=schema_diff_ignore_case,
                        ),
                        tasks_num=tasks_num,
                    )
                    self.console.stop_table_diff_progress(success=True)
                except:
                    self.console.stop_table_diff_progress(success=False)
                    raise
            elif selected_models:
                self.console.log_status_update(
                    f"No models contain differences with the selection criteria: {criteria}"
                )

        else:
            table_diffs = [
                self._table_diff(
                    source=source,
                    target=target,
                    source_alias=source,
                    target_alias=target,
                    limit=limit,
                    decimals=decimals,
                    adapter=self.engine_adapter,
                    on=on,
                    skip_columns=skip_columns,
                    where=where,
                    schema_diff_ignore_case=schema_diff_ignore_case,
                )
            ]

        if show:
            self.console.show_table_diff(table_diffs, show_sample, skip_grain_check, temp_schema)

        return table_diffs

    def _model_diff(
        self,
        model: Model,
        adapter: EngineAdapter,
        source: str,
        target: str,
        source_alias: str,
        target_alias: str,
        limit: int,
        decimals: int,
        on: t.Optional[t.List[str] | exp.Expr] = None,
        skip_columns: t.Optional[t.List[str]] = None,
        where: t.Optional[str | exp.Expr] = None,
        show: bool = True,
        temp_schema: t.Optional[str] = None,
        skip_grain_check: bool = False,
        schema_diff_ignore_case: bool = False,
    ) -> TableDiff:
        self.console.start_table_diff_model_progress(model.name)

        table_diff = self._table_diff(
            on=on,
            skip_columns=skip_columns,
            where=where,
            limit=limit,
            decimals=decimals,
            model=model,
            adapter=adapter,
            source=source,
            target=target,
            source_alias=source_alias,
            target_alias=target_alias,
            schema_diff_ignore_case=schema_diff_ignore_case,
        )

        if show:
            # Trigger row_diff in parallel execution so it's available for ordered display later
            table_diff.row_diff(temp_schema=temp_schema, skip_grain_check=skip_grain_check)

        self.console.update_table_diff_progress(model.name)

        return table_diff

    def _table_diff(
        self,
        source: str,
        target: str,
        source_alias: str,
        target_alias: str,
        limit: int,
        decimals: int,
        adapter: EngineAdapter,
        on: t.Optional[t.List[str] | exp.Expr] = None,
        model: t.Optional[Model] = None,
        skip_columns: t.Optional[t.List[str]] = None,
        where: t.Optional[str | exp.Expr] = None,
        schema_diff_ignore_case: bool = False,
    ) -> TableDiff:
        if not on:
            raise SQLMeshError(
                "SQLMesh doesn't know how to join the two tables. Specify the `grains` in each model definition or pass join column names in separate `-o` flags."
            )

        return TableDiff(
            adapter=adapter.with_settings(execute_log_level=logger.getEffectiveLevel()),
            source=source,
            target=target,
            on=on,
            skip_columns=skip_columns,
            where=where,
            source_alias=source_alias,
            target_alias=target_alias,
            limit=limit,
            decimals=decimals,
            model_name=model.name if model else None,
            model_dialect=model.dialect if model else None,
            schema_diff_ignore_case=schema_diff_ignore_case,
        )

    @python_api_analytics
    def get_dag(
        self, select_models: t.Optional[t.Collection[str]] = None, **options: t.Any
    ) -> GraphHTML:
        """Gets an HTML object representation of the DAG.

        Args:
            select_models: A list of model selection strings that should be included in the dag.
        Returns:
            An html object that renders the dag.
        """
        dag = (
            self.dag.prune(*self._new_selector().expand_model_selections(select_models))
            if select_models
            else self.dag
        )

        nodes = {}
        edges: t.List[t.Dict] = []

        for node, deps in dag.graph.items():
            nodes[node] = {
                "id": node,
                "label": node.split(".")[-1],
                "title": f"<span>{node}</span>",
            }
            edges.extend({"from": d, "to": node} for d in deps)

        return GraphHTML(
            nodes,
            edges,
            options={
                "height": "100%",
                "width": "100%",
                "interaction": {},
                "layout": {
                    "hierarchical": {
                        "enabled": True,
                        "nodeSpacing": 200,
                        "sortMethod": "directed",
                    },
                },
                "nodes": {
                    "shape": "box",
                },
                **options,
            },
        )

    @python_api_analytics
    def render_dag(self, path: str, select_models: t.Optional[t.Collection[str]] = None) -> None:
        """Render the dag as HTML and save it to a file.

        Args:
            path: filename to save the dag html to
            select_models: A list of model selection strings that should be included in the dag.
        """
        file_path = Path(path)
        suffix = file_path.suffix
        if suffix != ".html":
            if suffix:
                get_console().log_warning(
                    f"The extension {suffix} does not designate an html file. A file with a `.html` extension will be created instead."
                )
            path = str(file_path.with_suffix(".html"))

        with open(path, "w", encoding="utf-8") as file:
            file.write(str(self.get_dag(select_models)))

    @python_api_analytics
    def create_test(
        self,
        model: str,
        input_queries: t.Dict[str, str],
        overwrite: bool = False,
        variables: t.Optional[t.Dict[str, str]] = None,
        path: t.Optional[str] = None,
        name: t.Optional[str] = None,
        include_ctes: bool = False,
    ) -> None:
        """Generate a unit test fixture for a given model.

        Args:
            model: The model to test.
            input_queries: Mapping of model names to queries. Each model included in this mapping
                will be populated in the test based on the results of the corresponding query.
            overwrite: Whether to overwrite the existing test in case of a file path collision.
                When set to False, an error will be raised if there is such a collision.
            variables: Key-value pairs that will define variables needed by the model.
            path: The file path corresponding to the fixture, relative to the test directory.
                By default, the fixture will be created under the test directory and the file name
                will be inferred from the test's name.
            name: The name of the test. This is inferred from the model name by default.
            include_ctes: When true, CTE fixtures will also be generated.
        """
        input_queries = {
            # The get_model here has two purposes: return normalized names & check for missing deps
            self.get_model(dep, raise_if_missing=True).fqn: query
            for dep, query in input_queries.items()
        }

        try:
            model_to_test = self.get_model(model, raise_if_missing=True)
            test_adapter = self.test_connection_config.create_engine_adapter(
                register_comments_override=False
            )

            generate_test(
                model=model_to_test,
                input_queries=input_queries,
                models=self._models,
                engine_adapter=self._get_engine_adapter(model_to_test.gateway),
                test_engine_adapter=test_adapter,
                project_path=self.path,
                overwrite=overwrite,
                variables=variables,
                path=path,
                name=name,
                include_ctes=include_ctes,
            )
        finally:
            if test_adapter:
                test_adapter.close()

    @python_api_analytics
    def test(
        self,
        match_patterns: t.Optional[t.List[str]] = None,
        tests: t.Optional[t.List[str]] = None,
        verbosity: Verbosity = Verbosity.DEFAULT,
        preserve_fixtures: bool = False,
        stream: t.Optional[t.TextIO] = None,
    ) -> ModelTextTestResult:
        """Discover and run model tests"""
        if verbosity >= Verbosity.VERBOSE:
            import pandas as pd

            pd.set_option("display.max_columns", None)

        test_meta = self.select_tests(tests=tests, patterns=match_patterns)

        result = run_tests(
            model_test_metadata=test_meta,
            models=self._models,
            config=self.config,
            selected_gateway=self.selected_gateway,
            dialect=self.default_dialect,
            verbosity=verbosity,
            preserve_fixtures=preserve_fixtures,
            stream=stream,
            default_catalog=self.default_catalog,
            default_catalog_dialect=self.config.dialect or "",
        )

        self.console.log_test_results(
            result,
            self.test_connection_config._engine_adapter.DIALECT,
        )

        return result

    @python_api_analytics
    def audit(
        self,
        start: TimeLike,
        end: TimeLike,
        *,
        models: t.Optional[t.Iterator[str]] = None,
        execution_time: t.Optional[TimeLike] = None,
    ) -> bool:
        """Audit models.

        Args:
            start: The start of the interval to audit.
            end: The end of the interval to audit.
            models: The models to audit. All models will be audited if not specified.
            execution_time: The date/time time reference to use for execution time. Defaults to now.

        Returns:
            False if any of the audits failed, True otherwise.
        """

        snapshots = (
            [self.get_snapshot(model, raise_if_missing=True) for model in models]
            if models
            else self.snapshots.values()
        )

        num_audits = sum(len(snapshot.node.audits_with_args) for snapshot in snapshots)
        self.console.log_status_update(f"Found {num_audits} audit(s).")

        errors = []
        skipped_count = 0
        for snapshot in snapshots:
            for audit_result in self.snapshot_evaluator.audit(
                snapshot=snapshot,
                start=start,
                end=end,
                execution_time=execution_time,
                snapshots=self.snapshots,
            ):
                audit_id = f"{audit_result.audit.name}"
                if audit_result.model:
                    audit_id += f" on model {audit_result.model.name}"

                if audit_result.skipped:
                    self.console.log_status_update(f"{audit_id} ⏸️ SKIPPED.")
                    skipped_count += 1
                elif audit_result.count:
                    errors.append(audit_result)
                    self.console.log_status_update(
                        f"{audit_id} ❌ [red]FAIL [{audit_result.count}][/red]."
                    )
                else:
                    self.console.log_status_update(f"{audit_id} ✅ [green]PASS[/green].")

        self.console.log_status_update(
            f"\nFinished with {len(errors)} audit error{'' if len(errors) == 1 else 's'} "
            f"and {skipped_count} audit{'' if skipped_count == 1 else 's'} skipped."
        )
        for error in errors:
            self.console.log_status_update(
                f"\nFailure in audit {error.audit.name} ({error.audit._path})."
            )
            self.console.log_status_update(f"Got {error.count} results, expected 0.")
            if error.query:
                self.console.show_sql(
                    f"{error.query.sql(dialect=self.snapshot_evaluator.adapter.dialect)}"
                )

        self.console.log_status_update("Done.")
        return not errors

    @python_api_analytics
    def rewrite(self, sql: str, dialect: str = "") -> exp.Expr:
        """Rewrite a sql expression with semantic references into an executable query.

        https://sqlmesh.readthedocs.io/en/latest/concepts/metrics/overview/

        Args:
            sql: The sql string to rewrite.
            dialect: The dialect of the sql string, defaults to the project dialect.

        Returns:
            A SQLGlot expression with semantic references expanded.
        """
        return rewrite(
            sql,
            graph=ReferenceGraph(self.models.values()),
            metrics=self._metrics,
            dialect=dialect or self.default_dialect,
        )

    @python_api_analytics
    def check_intervals(
        self,
        environment: t.Optional[str],
        no_signals: bool,
        select_models: t.Collection[str],
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
    ) -> t.Dict[Snapshot, SnapshotIntervals]:
        """Check intervals for a given environment.

        Args:
            environment: The environment or prod if None.
            select_models: A list of model selection strings to show intervals for.
            start: The start of the intervals to check.
            end: The end of the intervals to check.
        """

        environment = environment or c.PROD
        env = self.state_reader.get_environment(environment)
        if not env:
            raise SQLMeshError(f"Environment '{environment}' was not found.")

        snapshots = {k.name: v for k, v in self.state_sync.get_snapshots(env.snapshots).items()}

        missing = {
            k.name: v
            for k, v in missing_intervals(
                snapshots.values(), start=start, end=end, execution_time=end
            ).items()
        }

        if select_models:
            selected: t.Collection[str] = self._select_models_for_run(
                select_models, True, snapshots.values()
            )
        else:
            selected = snapshots.keys()

        results = {}
        execution_context = self.execution_context(snapshots=snapshots)

        for fqn in selected:
            snapshot = snapshots[fqn]
            intervals = missing.get(fqn) or []

            results[snapshot] = SnapshotIntervals(
                snapshot.snapshot_id,
                intervals
                if no_signals
                else snapshot.check_ready_intervals(intervals, execution_context),
            )

        return results

    @python_api_analytics
    def migrate(self) -> None:
        """Migrates SQLMesh to the current running version.

        Please contact your SQLMesh administrator before doing this.
        """
        self.notification_target_manager.notify(NotificationEvent.MIGRATION_START)
        self._load_materializations()
        try:
            self._new_state_sync().migrate(
                promoted_snapshots_only=self.config.migration.promoted_snapshots_only,
            )
        except Exception as e:
            self.notification_target_manager.notify(
                NotificationEvent.MIGRATION_FAILURE, traceback.format_exc()
            )
            raise e
        self.notification_target_manager.notify(NotificationEvent.MIGRATION_END)

    @python_api_analytics
    def rollback(self) -> None:
        """Rolls back SQLMesh to the previous migration.

        Please contact your SQLMesh administrator before doing this. This action cannot be undone.
        """
        self._new_state_sync().rollback()

    @python_api_analytics
    def create_external_models(self, strict: bool = False) -> None:
        """Create a file to document the schema of external models.

        The external models file contains all columns and types of external models, allowing for more
        robust lineage, validation, and optimizations.

        Args:
            strict: If True, raise an error if the external model is missing in the database.
        """
        if not self._models:
            self.load(update_schemas=False)

        for path, config in self.configs.items():
            deprecated_yaml = path / c.EXTERNAL_MODELS_DEPRECATED_YAML

            external_models_yaml = (
                path / c.EXTERNAL_MODELS_YAML if not deprecated_yaml.exists() else deprecated_yaml
            )

            external_models_gateway: t.Optional[str] = self.gateway or self.config.default_gateway
            if not external_models_gateway:
                # can happen if there was no --gateway defined and the default_gateway is ''
                # which means that the single gateway syntax is being used which means there is
                # no named gateway which means we should not stamp `gateway:` on the external models
                external_models_gateway = None

            create_external_models_file(
                path=external_models_yaml,
                models=UniqueKeyDict(
                    "models",
                    {
                        fqn: model
                        for fqn, model in self._models.items()
                        if self.config_for_node(model) is config
                    },
                ),
                adapter=self.engine_adapter,
                state_reader=self.state_reader,
                dialect=config.model_defaults.dialect,
                gateway=external_models_gateway,
                max_workers=self.concurrent_tasks,
                strict=strict,
            )

    @python_api_analytics
    def print_info(
        self, skip_connection: bool = False, verbosity: Verbosity = Verbosity.DEFAULT
    ) -> None:
        """Prints information about connections, models, macros, etc. to the console."""
        self.console.log_status_update(f"Models: {len(self.models)}")
        self.console.log_status_update(f"Macros: {len(self._macros) - len(macro.get_registry())}")

        if skip_connection:
            return

        if verbosity >= Verbosity.VERBOSE:
            self.console.log_status_update("")
            print_config(self.config.get_connection(self.gateway), self.console, "Connection")
            print_config(
                self.config.get_test_connection(self.gateway), self.console, "Test Connection"
            )
            print_config(
                self.config.get_state_connection(self.gateway), self.console, "State Connection"
            )

        self._try_connection("data warehouse", self.engine_adapter.ping)
        state_connection = self.config.get_state_connection(self.gateway)
        if state_connection:
            self._try_connection("state backend", state_connection.connection_validator())

    @python_api_analytics
    def print_environment_names(self) -> None:
        """Prints all environment names along with expiry datetime."""
        result = self._new_state_sync().get_environments_summary()
        if not result:
            raise SQLMeshError(
                "This project has no environments. Create an environment using the `sqlmesh plan` command."
            )
        self.console.print_environments(result)

    def close(self) -> None:
        """Releases all resources allocated by this context."""
        if self._snapshot_evaluator:
            self._snapshot_evaluator.close()

        if self._state_sync:
            self._state_sync.close()

    def _run(
        self,
        environment: str,
        *,
        start: t.Optional[TimeLike],
        end: t.Optional[TimeLike],
        execution_time: t.Optional[TimeLike],
        ignore_cron: bool,
        select_models: t.Optional[t.Collection[str]],
        circuit_breaker: t.Optional[t.Callable[[], bool]],
        no_auto_upstream: bool,
    ) -> CompletionStatus:
        scheduler = self.scheduler(environment=environment)
        snapshots = scheduler.snapshots

        if select_models is not None:
            select_models = self._select_models_for_run(
                select_models, no_auto_upstream, snapshots.values()
            )

        completion_status = scheduler.run(
            environment,
            start=start,
            end=end,
            execution_time=execution_time,
            ignore_cron=ignore_cron,
            circuit_breaker=circuit_breaker,
            selected_snapshots=select_models,
            auto_restatement_enabled=environment.lower() == c.PROD,
            run_environment_statements=True,
        )

        if completion_status.is_nothing_to_do:
            next_run_ready_msg = ""

            next_ready_interval_start = get_next_model_interval_start(snapshots.values())
            if next_ready_interval_start:
                utc_time = format_tz_datetime(next_ready_interval_start)
                local_time = format_tz_datetime(next_ready_interval_start, use_local_timezone=True)
                time_msg = local_time if local_time == utc_time else f"{local_time} ({utc_time})"
                next_run_ready_msg = f"\n\nNext run will be ready at {time_msg}."

            self.console.log_status_update(
                f"No models are ready to run. Please wait until a model `cron` interval has elapsed.{next_run_ready_msg}"
            )

        return completion_status

    def _apply(self, plan: Plan, circuit_breaker: t.Optional[t.Callable[[], bool]]) -> None:
        self._scheduler.create_plan_evaluator(self).evaluate(
            plan.to_evaluatable(), circuit_breaker=circuit_breaker
        )

    @python_api_analytics
    def table_name(
        self, model_name: str, environment: t.Optional[str] = None, prod: bool = False
    ) -> str:
        """Returns the name of the pysical table for the given model name in the target environment.

        Args:
            model_name: The name of the model.
            environment: The environment to source the model version from.
            prod: If True, return the name of the physical table that will be used in production for the model version
                promoted in the target environment.

        Returns:
            The name of the physical table.
        """
        environment = environment or self.config.default_target_environment
        fqn = self._node_or_snapshot_to_fqn(model_name)
        target_env = self.state_reader.get_environment(environment)
        if not target_env:
            raise SQLMeshError(f"Environment '{environment}' was not found.")

        snapshot_info = None
        for s in target_env.snapshots:
            if s.name == fqn:
                snapshot_info = s
                break
        if not snapshot_info:
            raise SQLMeshError(
                f"Model '{model_name}' was not found in environment '{environment}'."
            )

        if target_env.name == c.PROD or prod:
            return snapshot_info.table_name()

        snapshots = self.state_reader.get_snapshots(target_env.snapshots)
        deployability_index = DeployabilityIndex.create(snapshots)

        return snapshot_info.table_name(
            is_deployable=deployability_index.is_deployable(snapshot_info.snapshot_id)
        )

    def clear_caches(self) -> None:
        paths_to_remove = [path / c.CACHE for path in self.configs]
        paths_to_remove.append(self.cache_dir)

        if IS_WINDOWS:
            paths_to_remove = [fix_windows_path(path) for path in paths_to_remove]

        for path in paths_to_remove:
            if path.exists():
                rmtree(path)

        if isinstance(self._state_sync, CachingStateSync):
            self._state_sync.clear_cache()

    def export_state(
        self,
        output_file: Path,
        environment_names: t.Optional[t.List[str]] = None,
        local_only: bool = False,
        confirm: bool = True,
    ) -> None:
        from sqlmesh.core.state_sync.export_import import export_state

        # trigger a connection to the StateSync so we can fail early if there is a problem
        # note we still need to do this even if we are doing a local export so we know what 'versions' to write
        self.state_sync.get_versions(validate=True)

        local_snapshots = self.snapshots if local_only else None

        if self.console.start_state_export(
            output_file=output_file,
            gateway=self.selected_gateway,
            state_connection_config=self._state_connection_config,
            environment_names=environment_names,
            local_only=local_only,
            confirm=confirm,
        ):
            try:
                export_state(
                    state_sync=self.state_sync,
                    output_file=output_file,
                    local_snapshots=local_snapshots,
                    environment_names=environment_names,
                    console=self.console,
                )
                self.console.stop_state_export(success=True, output_file=output_file)
            except:
                self.console.stop_state_export(success=False, output_file=output_file)
                raise

    def import_state(self, input_file: Path, clear: bool = False, confirm: bool = True) -> None:
        from sqlmesh.core.state_sync.export_import import import_state

        if self.console.start_state_import(
            input_file=input_file,
            gateway=self.selected_gateway,
            state_connection_config=self._state_connection_config,
            clear=clear,
            confirm=confirm,
        ):
            try:
                import_state(
                    state_sync=self.state_sync,
                    input_file=input_file,
                    clear=clear,
                    console=self.console,
                )
                self.console.stop_state_import(success=True, input_file=input_file)
            except:
                self.console.stop_state_import(success=False, input_file=input_file)
                raise

    def _run_tests(
        self, verbosity: Verbosity = Verbosity.DEFAULT
    ) -> t.Tuple[ModelTextTestResult, str]:
        test_output_io = StringIO()
        result = self.test(stream=test_output_io, verbosity=verbosity)
        return result, test_output_io.getvalue()

    def _run_plan_tests(self, skip_tests: bool = False) -> t.Optional[ModelTextTestResult]:
        if not skip_tests:
            result = self.test()
            if not result.wasSuccessful():
                raise PlanError(
                    "Cannot generate plan due to failing test(s). Fix test(s) and run again."
                )
            return result
        return None

    @property
    def _model_tables(self) -> t.Dict[str, str]:
        """Mapping of model name to physical table name.

        If a snapshot has not been versioned yet, its view name will be returned.
        """
        return {
            fqn: (
                snapshot.table_name()
                if snapshot.version
                else snapshot.qualified_view_name.for_environment(
                    EnvironmentNamingInfo.from_environment_catalog_mapping(
                        self.environment_catalog_mapping,
                        name=c.PROD,
                        suffix_target=self.config.environment_suffix_target,
                    )
                )
            )
            for fqn, snapshot in self.snapshots.items()
        }

    @cached_property
    def cache_dir(self) -> Path:
        if self.config.cache_dir:
            cache_path = Path(self.config.cache_dir)
            if cache_path.is_absolute():
                return cache_path
            return self.path / cache_path

        # Default to .cache directory in the project path
        return self.path / c.CACHE

    @cached_property
    def engine_adapters(self) -> t.Dict[str, EngineAdapter]:
        """Returns all the engine adapters for the gateways defined in the configurations."""
        adapters: t.Dict[str, EngineAdapter] = {self.selected_gateway: self.engine_adapter}
        for config in self.configs.values():
            for gateway_name in config.gateways:
                if gateway_name not in adapters:
                    connection = config.get_connection(gateway_name)
                    adapter = connection.create_engine_adapter(
                        concurrent_tasks=self.concurrent_tasks,
                    )
                    adapters[gateway_name] = adapter
        return adapters

    @cached_property
    def default_catalog_per_gateway(self) -> t.Dict[str, str]:
        """Returns the default catalogs for each engine adapter."""
        return self._scheduler.get_default_catalog_per_gateway(self)

    @property
    def concurrent_tasks(self) -> int:
        if self._concurrent_tasks is None:
            self._concurrent_tasks = self.connection_config.concurrent_tasks
        return self._concurrent_tasks

    @cached_property
    def connection_config(self) -> ConnectionConfig:
        return self.config.get_connection(self.selected_gateway)

    @cached_property
    def test_connection_config(self) -> ConnectionConfig:
        return self.config.get_test_connection(
            self.gateway,
            self.default_catalog,
            default_catalog_dialect=self.config.dialect,
        )

    @cached_property
    def environment_catalog_mapping(self) -> RegexKeyDict:
        engine_adapter = None
        try:
            engine_adapter = self.engine_adapter
        except Exception:
            pass

        if (
            self.config.environment_catalog_mapping
            and engine_adapter
            and not self.engine_adapter.catalog_support.is_multi_catalog_supported
        ):
            raise SQLMeshError(
                "Environment catalog mapping is only supported for engine adapters that support multiple catalogs"
            )
        return self.config.environment_catalog_mapping

    def _get_engine_adapter(self, gateway: t.Optional[str] = None) -> EngineAdapter:
        if gateway:
            if adapter := self.engine_adapters.get(gateway):
                return adapter
            raise SQLMeshError(f"Gateway '{gateway}' not found in the available engine adapters.")
        return self.engine_adapter

    def _snapshots(
        self, models_override: t.Optional[UniqueKeyDict[str, Model]] = None
    ) -> t.Dict[str, Snapshot]:
        nodes = {**(models_override or self._models), **self._standalone_audits}
        snapshots = self._nodes_to_snapshots(nodes)
        stored_snapshots = self.state_reader.get_snapshots(snapshots.values())

        unrestorable_snapshots = {
            snapshot
            for snapshot in stored_snapshots.values()
            if snapshot.name in nodes and snapshot.unrestorable
        }
        if unrestorable_snapshots:
            for snapshot in unrestorable_snapshots:
                logger.info(
                    "Found a unrestorable snapshot %s. Restamping the model...", snapshot.name
                )
                node = nodes[snapshot.name]
                nodes[snapshot.name] = node.copy(
                    update={"stamp": f"revert to {snapshot.identifier}"}
                )
            snapshots = self._nodes_to_snapshots(nodes)
            stored_snapshots = self.state_reader.get_snapshots(snapshots.values())

        for snapshot in stored_snapshots.values():
            # Keep the original model instance to preserve the query cache.
            snapshot.node = snapshots[snapshot.name].node

        return {name: stored_snapshots.get(s.snapshot_id, s) for name, s in snapshots.items()}

    def _context_diff(
        self,
        environment: str,
        snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
        create_from: t.Optional[str] = None,
        force_no_diff: bool = False,
        ensure_finalized_snapshots: bool = False,
        diff_rendered: bool = False,
        always_recreate_environment: bool = False,
    ) -> ContextDiff:
        environment = Environment.sanitize_name(environment)
        if force_no_diff:
            return ContextDiff.create_no_diff(environment, self.state_reader)

        return ContextDiff.create(
            environment,
            snapshots=snapshots or self.snapshots,
            create_from=create_from or c.PROD,
            state_reader=self.state_reader,
            provided_requirements=self._requirements,
            excluded_requirements=self._excluded_requirements,
            ensure_finalized_snapshots=ensure_finalized_snapshots,
            diff_rendered=diff_rendered,
            environment_statements=self._environment_statements,
            gateway_managed_virtual_layer=self.config.gateway_managed_virtual_layer,
            infer_python_dependencies=self.config.infer_python_dependencies,
            always_recreate_environment=always_recreate_environment,
        )

    def _destroy(self) -> bool:
        # Invalidate all environments, including prod
        for environment in self.state_reader.get_environments():
            self.state_sync.invalidate_environment(name=environment.name, protect_prod=False)
            self.console.log_success(f"Environment '{environment.name}' invalidated.")

        # Run janitor to clean up all objects
        self._run_janitor(ignore_ttl=True)

        # Remove state tables, including backup tables
        self.state_sync.remove_state(including_backup=True)
        self.console.log_status_update("State tables removed.")

        # Finally clear caches
        self.clear_caches()

        return True

    def _run_janitor(self, ignore_ttl: bool = False) -> None:
        current_ts = now_timestamp()

        # Clean up expired environments by removing their views and schemas
        self._cleanup_environments(current_ts=current_ts)

        delete_expired_snapshots(
            self.state_sync,
            self.snapshot_evaluator,
            current_ts=current_ts,
            ignore_ttl=ignore_ttl,
            console=self.console,
            batch_size=self.config.janitor.expired_snapshots_batch_size,
        )
        self.state_sync.compact_intervals()

    def _cleanup_environments(self, current_ts: t.Optional[int] = None) -> None:
        current_ts = current_ts or now_timestamp()

        expired_environments_summaries = self.state_sync.get_expired_environments(
            current_ts=current_ts
        )

        for expired_env_summary in expired_environments_summaries:
            expired_env = self.state_reader.get_environment(expired_env_summary.name)

            if expired_env:
                cleanup_expired_views(
                    default_adapter=self.engine_adapter,
                    engine_adapters=self.engine_adapters,
                    environments=[expired_env],
                    warn_on_delete_failure=self.config.janitor.warn_on_delete_failure,
                    console=self.console,
                )

        self.state_sync.delete_expired_environments(current_ts=current_ts)

    def _try_connection(self, connection_name: str, validator: t.Callable[[], None]) -> None:
        connection_name = connection_name.capitalize()
        try:
            validator()
            self.console.log_status_update(f"{connection_name} connection [green]succeeded[/green]")
        except Exception as ex:
            self.console.log_error(f"{connection_name} connection failed. {ex}")

    def _new_state_sync(self) -> StateSync:
        return self._provided_state_sync or self._scheduler.create_state_sync(self)

    def _new_selector(
        self, models: t.Optional[UniqueKeyDict[str, Model]] = None, dag: t.Optional[DAG[str]] = None
    ) -> Selector:
        return self._selector_cls(
            self.state_reader,
            models=models or self._models,
            context_path=self.path,
            dag=dag,
            default_catalog=self.default_catalog,
            dialect=self.default_dialect,
            cache_dir=self.cache_dir,
        )

    def _register_notification_targets(self) -> None:
        event_notifications = collections.defaultdict(set)
        for target in self.notification_targets:
            if target.is_configured:
                for event in target.notify_on:
                    event_notifications[event].add(target)
        user_notification_targets = {
            user.username: set(
                target for target in user.notification_targets if target.is_configured
            )
            for user in self.users
        }
        self.notification_target_manager = NotificationTargetManager(
            event_notifications, user_notification_targets, username=self.config.username
        )

    def _load_materializations(self) -> None:
        if not self._loaded:
            for loader in self._loaders:
                loader.load_materializations()

    def _select_models_for_run(
        self,
        select_models: t.Collection[str],
        no_auto_upstream: bool,
        snapshots: t.Collection[Snapshot],
    ) -> t.Set[str]:
        models: UniqueKeyDict[str, Model] = UniqueKeyDict(
            "models", **{s.name: s.model for s in snapshots if s.is_model}
        )
        dag: DAG[str] = DAG()
        for fqn, model in models.items():
            dag.add(fqn, model.depends_on)
        model_selector = self._new_selector(models=models, dag=dag)
        result = set(model_selector.expand_model_selections(select_models))
        if not no_auto_upstream:
            result = set(dag.subdag(*result))
        return result

    @cached_property
    def _project_type(self) -> str:
        project_types = {
            c.DBT if loader.__class__.__name__.lower().startswith(c.DBT) else c.NATIVE
            for loader in self._loaders
        }
        return c.HYBRID if len(project_types) > 1 else first(project_types)

    def _nodes_to_snapshots(self, nodes: t.Dict[str, Node]) -> t.Dict[str, Snapshot]:
        snapshots: t.Dict[str, Snapshot] = {}
        fingerprint_cache: t.Dict[str, SnapshotFingerprint] = {}

        for node in nodes.values():
            kwargs: t.Dict[str, t.Any] = {}
            if node.project in self._projects:
                config = self.config_for_node(node)
                kwargs["ttl"] = config.snapshot_ttl
                kwargs["table_naming_convention"] = config.physical_table_naming_convention

            snapshot = Snapshot.from_node(
                node,
                nodes=nodes,
                cache=fingerprint_cache,
                **kwargs,
            )
            snapshots[snapshot.name] = snapshot
        return snapshots

    def _node_or_snapshot_to_fqn(self, node_or_snapshot: NodeOrSnapshot) -> str:
        if isinstance(node_or_snapshot, Snapshot):
            return node_or_snapshot.name
        if isinstance(node_or_snapshot, str) and not self.standalone_audits.get(node_or_snapshot):
            return normalize_model_name(
                node_or_snapshot,
                dialect=self.default_dialect,
                default_catalog=self.default_catalog,
            )
        if not isinstance(node_or_snapshot, str):
            return node_or_snapshot.fqn
        return node_or_snapshot

    @property
    def _plan_preview_enabled(self) -> bool:
        if self.config.plan.enable_preview is not None:
            return self.config.plan.enable_preview
        # It is dangerous to enable preview by default for dbt projects that rely on engines that don't support cloning.
        # Enabling previews in such cases can result in unintended full refreshes because dbt incremental models rely on
        # the maximum timestamp value in the target table.
        return self._project_type == c.NATIVE or self.engine_adapter.SUPPORTS_CLONING

    def _get_plan_default_start_end(
        self,
        snapshots: t.Dict[str, Snapshot],
        max_interval_end_per_model: t.Dict[str, datetime],
        backfill_models: t.Optional[t.Set[str]],
        modified_model_names: t.Set[str],
        execution_time: t.Optional[TimeLike] = None,
    ) -> t.Tuple[t.Optional[int], t.Optional[int]]:
        # exclude seeds so their stale interval ends does not become the default plan end date
        # when they're the only ones that contain intervals in this plan
        non_seed_interval_ends = {
            model_fqn: end
            for model_fqn, end in max_interval_end_per_model.items()
            if model_fqn not in snapshots or not snapshots[model_fqn].is_seed
        }
        if not non_seed_interval_ends:
            return None, None

        default_end = to_timestamp(max(non_seed_interval_ends.values()))
        default_start: t.Optional[int] = None
        # Infer the default start by finding the smallest interval start that corresponds to the default end.
        for model_name in backfill_models or modified_model_names or max_interval_end_per_model:
            if model_name not in snapshots:
                continue
            node = snapshots[model_name].node
            interval_unit = node.interval_unit
            default_start = min(
                default_start or sys.maxsize,
                to_timestamp(
                    interval_unit.cron_prev(
                        interval_unit.cron_floor(
                            max_interval_end_per_model.get(
                                model_name, node.cron_floor(default_end)
                            ),
                        ),
                        estimate=True,
                    )
                ),
            )

        if execution_time and to_timestamp(default_end) > to_timestamp(execution_time):
            # the end date can't be in the future, which can happen if a specific `execution_time` is set and prod intervals
            # are newer than it
            default_end = to_timestamp(execution_time)

        return default_start, default_end

    def _calculate_start_override_per_model(
        self,
        min_intervals: t.Optional[int],
        plan_start: t.Optional[TimeLike],
        plan_end: t.Optional[TimeLike],
        plan_execution_time: TimeLike,
        backfill_model_fqns: t.Optional[t.Set[str]],
        snapshots_by_model_fqn: t.Dict[str, Snapshot],
        end_override_per_model: t.Optional[t.Dict[str, datetime]],
    ) -> t.Dict[str, datetime]:
        if not min_intervals or not backfill_model_fqns or not plan_start:
            # If there are no models to backfill, there are no intervals to consider for backfill, so we dont need to consider a minimum number
            # If the plan doesnt have a start date, all intervals are considered already so we dont need to consider a minimum number
            # If we dont have a minimum number of intervals to consider, then we dont need to adjust the start date on a per-model basis
            return {}

        start_overrides: t.Dict[str, datetime] = {}
        end_override_per_model = end_override_per_model or {}

        plan_execution_time_dt = to_datetime(plan_execution_time)
        plan_start_dt = to_datetime(plan_start, relative_base=plan_execution_time_dt)
        plan_end_dt = to_datetime(
            plan_end or plan_execution_time_dt, relative_base=plan_execution_time_dt
        )

        # we need to take the DAG into account so that parent models can be expanded to cover at least as much as their children
        # for example, A(hourly) <- B(daily)
        # if min_intervals=1, A would have 1 hour and B would have 1 day
        # but B depends on A so in order for B to have 1 valid day, A needs to be expanded to 24 hours
        backfill_dag: DAG[str] = DAG()
        for fqn in backfill_model_fqns:
            backfill_dag.add(
                fqn,
                [
                    p.name
                    for p in snapshots_by_model_fqn[fqn].parents
                    if p.name in backfill_model_fqns
                ],
            )

        # start from the leaf nodes and work back towards the root because the min_start at the root node is determined by the calculated starts in the leaf nodes
        reversed_dag = backfill_dag.reversed
        graph = reversed_dag.graph

        for model_fqn in reversed_dag:
            # Get the earliest start from all immediate children of this snapshot
            # this works because topological ordering guarantees that they've already been visited
            # and we always set a start override
            min_child_start = min(
                [start_overrides[immediate_child_fqn] for immediate_child_fqn in graph[model_fqn]],
                default=plan_start_dt,
            )

            snapshot = snapshots_by_model_fqn.get(model_fqn)

            if not snapshot:
                continue

            starting_point = end_override_per_model.get(model_fqn, plan_end_dt)
            if node_end := snapshot.node.end:
                # if we dont do this, if the node end is a *date* (as opposed to a timestamp)
                # we end up incorrectly winding back an extra day
                node_end_dt = make_exclusive(node_end)

                if node_end_dt < plan_end_dt:
                    # if the model has an end date that has already elapsed, use that as a starting point for calculating min_intervals
                    # instead of the plan end. If we use the plan end, we will return intervals in the future which are invalid
                    starting_point = node_end_dt

            snapshot_start = snapshot.node.cron_floor(starting_point)

            for _ in range(min_intervals):
                # wind back the starting point by :min_intervals intervals to arrive at the minimum snapshot start date
                snapshot_start = snapshot.node.cron_prev(snapshot_start)

            start_overrides[model_fqn] = min(min_child_start, snapshot_start)

        return start_overrides

    def _get_max_interval_end_per_model(
        self, snapshots: t.Dict[str, Snapshot], backfill_models: t.Optional[t.Set[str]]
    ) -> t.Dict[str, datetime]:
        models_for_interval_end = (
            self._get_models_for_interval_end(snapshots, backfill_models)
            if backfill_models is not None
            else None
        )
        return {
            model_fqn: to_datetime(ts)
            for model_fqn, ts in self.state_sync.max_interval_end_per_model(
                c.PROD,
                models=models_for_interval_end,
                ensure_finalized_snapshots=self.config.plan.use_finalized_state,
            ).items()
        }

    @staticmethod
    def _get_models_for_interval_end(
        snapshots: t.Dict[str, Snapshot], backfill_models: t.Set[str]
    ) -> t.Set[str]:
        models_for_interval_end = set()
        models_stack = list(backfill_models)
        while models_stack:
            next_model = models_stack.pop()
            if next_model not in snapshots:
                continue
            models_for_interval_end.add(next_model)
            models_stack.extend(
                s.name
                for s in snapshots[next_model].parents
                if s.name not in models_for_interval_end
            )
        return models_for_interval_end

    def lint_models(
        self,
        models: t.Optional[t.Iterable[t.Union[str, Model]]] = None,
        raise_on_error: bool = True,
    ) -> t.List[AnnotatedRuleViolation]:
        found_error = False

        model_list = (
            list(self.get_model(model, raise_if_missing=True) for model in models)
            if models
            else self.models.values()
        )
        all_violations = []
        for model in model_list:
            # Linter may be `None` if the context is not loaded yet
            if linter := self._linters.get(model.project):
                lint_violation, violations = (
                    linter.lint_model(model, self, console=self.console) or found_error
                )
                if lint_violation:
                    found_error = True
                all_violations.extend(violations)

        if raise_on_error and found_error:
            raise LinterError(
                "Linter detected errors in the code. Please fix them before proceeding."
            )

        return all_violations

    def select_tests(
        self,
        tests: t.Optional[t.List[str]] = None,
        patterns: t.Optional[t.List[str]] = None,
    ) -> t.List[ModelTestMetadata]:
        """Filter pre-loaded test metadata based on tests and patterns."""

        test_meta = self._model_test_metadata

        if tests:
            filtered_tests = []
            for test in tests:
                if "::" in test:
                    if test in self._model_test_metadata_fully_qualified_name_index:
                        filtered_tests.append(
                            self._model_test_metadata_fully_qualified_name_index[test]
                        )
                else:
                    test_path = Path(test)
                    if test_path in self._model_test_metadata_path_index:
                        filtered_tests.extend(self._model_test_metadata_path_index[test_path])

            test_meta = filtered_tests

        if patterns:
            test_meta = filter_tests_by_patterns(test_meta, patterns)

        return test_meta


class Context(GenericContext[Config]):
    CONFIG_TYPE = Config
