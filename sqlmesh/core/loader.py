from __future__ import annotations

import abc
import glob
import itertools
import linecache
import os
import re
import typing as t
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from pydantic import ValidationError
import concurrent.futures

from sqlglot.errors import SqlglotError
from sqlglot import exp
from sqlglot.helper import subclasses

from sqlmesh.core import constants as c
from sqlmesh.core.audit import Audit, ModelAudit, StandaloneAudit, load_multiple_audits
from sqlmesh.core.console import Console
from sqlmesh.core.dialect import parse
from sqlmesh.core.environment import EnvironmentStatements
from sqlmesh.core.linter.rule import Rule
from sqlmesh.core.linter.definition import RuleSet
from sqlmesh.core.macros import MacroRegistry, macro
from sqlmesh.core.metric import Metric, MetricMeta, expand_metrics, load_metric_ddl
from sqlmesh.core.model import (
    Model,
    ModelCache,
    create_external_model,
    load_sql_based_models,
)
from sqlmesh.core.model import model as model_registry
from sqlmesh.core.model.common import make_python_env
from sqlmesh.core.signal import signal
from sqlmesh.core.test import ModelTestMetadata
from sqlmesh.utils import UniqueKeyDict, sys_path
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.jinja import JinjaMacroRegistry, MacroExtractor
from sqlmesh.utils.metaprogramming import import_python_file
from sqlmesh.utils.pydantic import validation_error_message
from sqlmesh.utils.process import create_process_pool_executor
from sqlmesh.utils.yaml import YAML, load as yaml_load


if t.TYPE_CHECKING:
    from sqlmesh.core.context import GenericContext


GATEWAY_PATTERN = re.compile(r"gateway:\s*([^\s]+)")


@dataclass
class LoadedProject:
    macros: MacroRegistry
    jinja_macros: JinjaMacroRegistry
    models: UniqueKeyDict[str, Model]
    standalone_audits: UniqueKeyDict[str, StandaloneAudit]
    audits: UniqueKeyDict[str, ModelAudit]
    metrics: UniqueKeyDict[str, Metric]
    requirements: t.Dict[str, str]
    excluded_requirements: t.Set[str]
    environment_statements: t.List[EnvironmentStatements]
    user_rules: RuleSet
    model_test_metadata: t.List[ModelTestMetadata]


class CacheBase(abc.ABC):
    @abc.abstractmethod
    def get_or_load_models(
        self, target_path: Path, loader: t.Callable[[], t.List[Model]]
    ) -> t.List[Model]:
        """Get or load all models from cache."""
        pass

    @abc.abstractmethod
    def put(self, models: t.List[Model], path: Path) -> bool:
        """Store models in the cache associated with the given path.

        Args:
            models: List of models to cache
            path: File path to associate with the cached models

        Returns:
            True if the models were successfully cached,
            False otherwise (empty list, not a list, unsupported model types)
        """
        pass

    @abc.abstractmethod
    def get(self, path: Path) -> t.List[Model]:
        """Retrieve models from the cache for a given path.

        Args:
            path: File path to look up in the cache

        Returns:
            List of cached models associated with the path, an empty list if no cache entry exists
        """
        pass


_defaults: t.Optional[t.Dict[str, t.Any]] = None
_cache: t.Optional[CacheBase] = None
_config_essentials: t.Optional[t.Dict[str, t.Any]] = None
_selected_gateway: t.Optional[str] = None


def _init_model_defaults(
    config_essentials: t.Dict[str, t.Any],
    selected_gateway: t.Optional[str],
    model_loading_defaults: t.Optional[t.Dict[str, t.Any]] = None,
    cache: t.Optional[CacheBase] = None,
    console: t.Optional[Console] = None,
) -> None:
    global _defaults, _cache, _config_essentials, _selected_gateway
    _defaults = model_loading_defaults
    _cache = cache
    _config_essentials = config_essentials
    _selected_gateway = selected_gateway

    # Set the console passed from the parent process
    if console is not None:
        from sqlmesh.core.console import set_console

        set_console(console)


def load_sql_models(path: Path) -> t.List[Model]:
    assert _defaults
    assert _cache

    with open(path, "r", encoding="utf-8") as file:
        expressions = parse(file.read(), default_dialect=_defaults["dialect"])
    models = load_sql_based_models(expressions, path=Path(path).absolute(), **_defaults)

    return [] if _cache.put(models, path) else models


def get_variables(gateway_name: t.Optional[str] = None) -> t.Dict[str, t.Any]:
    assert _config_essentials

    gateway_name = gateway_name or _selected_gateway

    try:
        gateway = _config_essentials["gateways"].get(gateway_name)
    except ConfigError:
        from sqlmesh.core.console import get_console

        get_console().log_warning(
            f"Gateway '{gateway_name}' not found in project '{_config_essentials['project']}'."
        )
        gateway = None

    return {
        **_config_essentials["variables"],
        **(gateway.variables if gateway else {}),
        c.GATEWAY: gateway_name,
    }


class Loader(abc.ABC):
    """Abstract base class to load macros and models for a context"""

    def __init__(self, context: GenericContext, path: Path) -> None:
        # This ensures pandas is imported before any model loading happens in the forked process
        # to avoid macOS fork() safety issues, see https://stackoverflow.com/a/52230415. Without
        # it, the following error was observerd in a macOS 15.5 system:
        #
        # "+[NSMutableString initialize] may have been in progress in another thread when fork() was called."
        import pandas as pd  # noqa

        from sqlmesh.core.console import get_console

        self._path_mtimes: t.Dict[Path, float] = {}
        self.context = context
        self.config_path = path
        self.config = self.context.configs[self.config_path]
        self._variables_by_gateway: t.Dict[str, t.Dict[str, t.Any]] = {}
        self._console = get_console()

        self.config_essentials = {
            "project": self.config.project,
            "variables": self.config.variables,
            "gateways": self.config.gateways,
        }
        _init_model_defaults(self.config_essentials, self.context.selected_gateway)

    def load(self) -> LoadedProject:
        """
        Loads all macros and models in the context's path.

        Returns:
            A loaded project object.
        """
        with sys_path(self.config_path):
            # python files are cached by the system
            # need to manually clear here so we can reload macros
            linecache.clearcache()
            self._path_mtimes.clear()

            self._load_materializations()
            signals = self._load_signals()

            config_mtimes: t.Dict[Path, t.List[float]] = defaultdict(list)

            for config_file in self.config_path.glob("config.*"):
                self._track_file(config_file)
                config_mtimes[self.config_path].append(self._path_mtimes[config_file])

            for config_file in c.SQLMESH_PATH.glob("config.*"):
                self._track_file(config_file)
                config_mtimes[c.SQLMESH_PATH].append(self._path_mtimes[config_file])

            self._config_mtimes = {path: max(mtimes) for path, mtimes in config_mtimes.items()}

            macros, jinja_macros = self._load_scripts()
            audits: UniqueKeyDict[str, ModelAudit] = UniqueKeyDict("audits")
            standalone_audits: UniqueKeyDict[str, StandaloneAudit] = UniqueKeyDict(
                "standalone_audits"
            )

            for name, audit in self._load_audits(macros=macros, jinja_macros=jinja_macros).items():
                if isinstance(audit, ModelAudit):
                    audits[name] = audit
                else:
                    standalone_audits[name] = audit

            models = self._load_models(
                macros,
                jinja_macros,
                self.context.selected_gateway,
                audits,
                signals,
            )

            metrics = self._load_metrics()

            requirements, excluded_requirements = self._load_requirements()

            environment_statements = self._load_environment_statements(macros=macros)

            user_rules = self._load_linting_rules()

            model_test_metadata = self.load_model_tests()

            project = LoadedProject(
                macros=macros,
                jinja_macros=jinja_macros,
                models=models,
                audits=audits,
                standalone_audits=standalone_audits,
                metrics=expand_metrics(metrics),
                requirements=requirements,
                excluded_requirements=excluded_requirements,
                environment_statements=environment_statements,
                user_rules=user_rules,
                model_test_metadata=model_test_metadata,
            )
            return project

    def reload_needed(self) -> bool:
        """
        Checks for any modifications to the files the macros and models depend on
        since the last load.

        Returns:
            True if a modification is found; False otherwise
        """
        return any(
            not path.exists() or path.stat().st_mtime > initial_mtime
            for path, initial_mtime in self._path_mtimes.copy().items()
        )

    @abc.abstractmethod
    def _load_scripts(self) -> t.Tuple[MacroRegistry, JinjaMacroRegistry]:
        """Loads all user defined macros."""

    @abc.abstractmethod
    def _load_models(
        self,
        macros: MacroRegistry,
        jinja_macros: JinjaMacroRegistry,
        gateway: t.Optional[str],
        audits: UniqueKeyDict[str, ModelAudit],
        signals: UniqueKeyDict[str, signal],
    ) -> UniqueKeyDict[str, Model]:
        """Loads all models."""

    @abc.abstractmethod
    def _load_audits(
        self, macros: MacroRegistry, jinja_macros: JinjaMacroRegistry
    ) -> UniqueKeyDict[str, Audit]:
        """Loads all audits."""

    def _load_environment_statements(self, macros: MacroRegistry) -> t.List[EnvironmentStatements]:
        """Loads environment statements."""
        return []

    def load_materializations(self) -> None:
        """Loads custom materializations."""

    def _load_materializations(self) -> None:
        pass

    def _load_signals(self) -> UniqueKeyDict[str, signal]:
        return UniqueKeyDict("signals")

    def _load_metrics(self) -> UniqueKeyDict[str, MetricMeta]:
        return UniqueKeyDict("metrics")

    def _load_external_models(
        self,
        audits: UniqueKeyDict[str, ModelAudit],
        cache: CacheBase,
        gateway: t.Optional[str] = None,
    ) -> UniqueKeyDict[str, Model]:
        models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
        external_models_yaml = Path(self.config_path / c.EXTERNAL_MODELS_YAML)
        deprecated_yaml = Path(self.config_path / c.EXTERNAL_MODELS_DEPRECATED_YAML)
        external_models_path = self.config_path / c.EXTERNAL_MODELS

        paths_to_load = []
        if external_models_yaml.exists():
            paths_to_load.append(external_models_yaml)
        elif deprecated_yaml.exists():
            paths_to_load.append(deprecated_yaml)

        if external_models_path.exists() and external_models_path.is_dir():
            paths_to_load.extend(self._glob_paths(external_models_path, extension=".yaml"))

        def _load(path: Path) -> t.List[Model]:
            try:
                with open(path, "r", encoding="utf-8") as file:
                    yaml = YAML().load(file)
                    # Allow empty YAML files to return an empty list
                    if yaml is None:
                        return []
                    return [
                        create_external_model(
                            defaults=self.config.model_defaults.dict(),
                            path=path,
                            project=self.config.project,
                            audit_definitions=audits,
                            **{
                                "dialect": self.config.model_defaults.dialect,
                                "default_catalog": self.context.default_catalog,
                                **row,
                            },
                        )
                        for row in yaml
                    ]
            except Exception as ex:
                raise ConfigError(self._failed_to_load_model_error(path, ex), path)

        for path in paths_to_load:
            self._track_file(path)

            external_models = cache.get_or_load_models(path, lambda: _load(path))

            # external models with no explicit gateway defined form the base set
            for model in external_models:
                if model.gateway is None:
                    if model.fqn in models:
                        raise ConfigError(
                            self._failed_to_load_model_error(
                                path, f"Duplicate external model name: '{model.name}'."
                            ),
                            path,
                        )
                    models[model.fqn] = model

            # however, if there is a gateway defined, gateway-specific models take precedence
            if gateway:
                gateway = gateway.lower()
                for model in external_models:
                    if model.gateway == gateway:
                        if model.fqn in models and models[model.fqn].gateway == gateway:
                            raise ConfigError(
                                self._failed_to_load_model_error(
                                    path, f"Duplicate external model name: '{model.name}'."
                                ),
                                path,
                            )
                        models.update({model.fqn: model})

        return models

    def _load_requirements(self) -> t.Tuple[t.Dict[str, str], t.Set[str]]:
        """Loads Python dependencies from the lock file.

        Returns:
            A tuple of requirements and excluded requirements.
        """
        requirements: t.Dict[str, str] = {}
        excluded_requirements: t.Set[str] = set()

        requirements_path = self.config_path / c.REQUIREMENTS
        if requirements_path.is_file():
            with open(requirements_path, "r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if line.startswith("^"):
                        excluded_requirements.add(line[1:])
                        continue

                    args = [k.strip() for k in line.split("==")]
                    if len(args) != 2:
                        raise ConfigError(
                            f"Invalid lock file entry '{line.strip()}'. Only 'dep==ver' is supported",
                            requirements_path,
                        )
                    dep, ver = args
                    other_ver = requirements.get(dep, ver)
                    if ver != other_ver:
                        raise ConfigError(
                            f"Conflicting requirement {dep}: {ver} != {other_ver}. Fix your {c.REQUIREMENTS} file.",
                            requirements_path,
                        )
                    requirements[dep] = ver

        return requirements, excluded_requirements

    def _load_linting_rules(self) -> RuleSet:
        """Loads user linting rules"""
        return RuleSet()

    def load_model_tests(self) -> t.List[ModelTestMetadata]:
        """Loads YAML-based model tests"""
        return []

    def _glob_paths(
        self,
        path: Path,
        ignore_patterns: t.Optional[t.List[str]] = None,
        extension: t.Optional[str] = None,
    ) -> t.Generator[Path, None, None]:
        """
        Globs the provided path for the file extension but also removes any filepaths that match an ignore
        pattern either set in constants or provided in config

        Args:
            path: The filepath to glob
            ignore_patterns: A list of patterns for glob to ignore
            extension: The extension to check for in that path (checks recursively in zero or more subdirectories)

        Returns:
            Matched paths that are not ignored
        """
        ignore_patterns = ignore_patterns or []
        extension = extension or ""

        # We try to match both ignore_pattern itself and every file returned by glob,
        # so that we will always ignore file names that do not appear in the latter.
        ignored_filepaths = set(ignore_patterns) | {
            ignored_path
            for ignore_pattern in ignore_patterns
            for ignored_path in glob.glob(str(self.config_path / ignore_pattern), recursive=True)
        }
        for filepath in path.glob(f"**/*{extension}"):
            if any(filepath.match(ignored_filepath) for ignored_filepath in ignored_filepaths):
                continue

            yield filepath

    def _track_file(self, path: Path) -> None:
        """Project file to track for modifications"""
        self._path_mtimes[path] = path.stat().st_mtime

    def _failed_to_load_model_error(self, path: Path, error: t.Union[str, Exception]) -> str:
        base_message = f"Failed to load model from file '{path}':"
        if isinstance(error, ValidationError):
            return validation_error_message(error, base_message)
        # indent all lines of error message
        error_message = str(error).replace("\n", "\n  ")
        return f"{base_message}\n\n  {error_message}"


class SqlMeshLoader(Loader):
    """Loads macros and models for a context using the SQLMesh file formats"""

    def _load_scripts(self) -> t.Tuple[MacroRegistry, JinjaMacroRegistry]:
        """Loads all user defined macros."""
        # Store a copy of the macro registry
        standard_macros = macro.get_registry()
        jinja_macros = JinjaMacroRegistry()
        extractor = MacroExtractor()

        macros_max_mtime: t.Optional[float] = None

        for path in self._glob_paths(
            self.config_path / c.MACROS,
            ignore_patterns=self.config.ignore_patterns,
            extension=".py",
        ):
            if import_python_file(path, self.config_path):
                self._track_file(path)
                macro_file_mtime = self._path_mtimes[path]
                macros_max_mtime = (
                    max(macros_max_mtime, macro_file_mtime)
                    if macros_max_mtime
                    else macro_file_mtime
                )

        for path in self._glob_paths(
            self.config_path / c.MACROS,
            ignore_patterns=self.config.ignore_patterns,
            extension=".sql",
        ):
            self._track_file(path)
            macro_file_mtime = self._path_mtimes[path]
            macros_max_mtime = (
                max(macros_max_mtime, macro_file_mtime) if macros_max_mtime else macro_file_mtime
            )
            with open(path, "r", encoding="utf-8") as file:
                jinja_macros.add_macros(
                    extractor.extract(file.read(), dialect=self.config.model_defaults.dialect)
                )

        self._macros_max_mtime = macros_max_mtime

        macros = macro.get_registry()
        macro.set_registry(standard_macros)

        return macros, jinja_macros

    def _load_models(
        self,
        macros: MacroRegistry,
        jinja_macros: JinjaMacroRegistry,
        gateway: t.Optional[str],
        audits: UniqueKeyDict[str, ModelAudit],
        signals: UniqueKeyDict[str, signal],
    ) -> UniqueKeyDict[str, Model]:
        """
        Loads all of the models within the model directory with their associated
        audits into a Dict and creates the dag
        """
        cache = SqlMeshLoader._Cache(self, self.config_path)

        sql_models = self._load_sql_models(macros, jinja_macros, audits, signals, cache, gateway)
        external_models = self._load_external_models(audits, cache, gateway)
        python_models = self._load_python_models(macros, jinja_macros, audits, signals)

        all_model_names = list(sql_models) + list(external_models) + list(python_models)
        duplicates = [name for name, count in Counter(all_model_names).items() if count > 1]
        if duplicates:
            raise ConfigError(f"Duplicate model name(s) found: {', '.join(duplicates)}.")

        return UniqueKeyDict("models", **sql_models, **external_models, **python_models)

    def _load_sql_models(
        self,
        macros: MacroRegistry,
        jinja_macros: JinjaMacroRegistry,
        audits: UniqueKeyDict[str, ModelAudit],
        signals: UniqueKeyDict[str, signal],
        cache: CacheBase,
        gateway: t.Optional[str],
    ) -> UniqueKeyDict[str, Model]:
        """Loads the sql models into a Dict"""
        models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
        paths: t.Set[Path] = set()
        cached_paths: UniqueKeyDict[Path, t.List[Model]] = UniqueKeyDict("cached_paths")

        for path in self._glob_paths(
            self.config_path / c.MODELS,
            ignore_patterns=self.config.ignore_patterns,
            extension=".sql",
        ):
            if not os.path.getsize(path):
                continue

            self._track_file(path)
            paths.add(path)
            if cached_models := cache.get(path):
                cached_paths[path] = cached_models

        for path, cached_models in cached_paths.items():
            paths.remove(path)
            for model in cached_models:
                if model.enabled:
                    models[model.fqn] = model

        if paths:
            model_loading_defaults = dict(
                get_variables=get_variables,
                defaults=self.config.model_defaults.dict(),
                macros=macros,
                jinja_macros=jinja_macros,
                audit_definitions=audits,
                module_path=self.config_path,
                dialect=self.config.model_defaults.dialect,
                time_column_format=self.config.time_column_format,
                physical_schema_mapping=self.config.physical_schema_mapping,
                project=self.config.project,
                default_catalog=self.context.default_catalog,
                infer_names=self.config.model_naming.infer_names,
                signal_definitions=signals,
                default_catalog_per_gateway=self.context.default_catalog_per_gateway,
                virtual_environment_mode=self.config.virtual_environment_mode,
            )

            with create_process_pool_executor(
                initializer=_init_model_defaults,
                initargs=(
                    self.config_essentials,
                    gateway,
                    model_loading_defaults,
                    cache,
                    self._console,
                ),
                max_workers=c.MAX_FORK_WORKERS,
            ) as pool:
                futures_to_paths = {pool.submit(load_sql_models, path): path for path in paths}
                for future in concurrent.futures.as_completed(futures_to_paths):
                    path = futures_to_paths[future]
                    try:
                        loaded = future.result()
                        for model in loaded or cache.get(path):
                            if model.fqn in models:
                                raise ConfigError(
                                    self._failed_to_load_model_error(
                                        path, f"Duplicate SQL model name: '{model.name}'."
                                    ),
                                    path,
                                )
                            elif model.enabled:
                                model._path = path
                                models[model.fqn] = model
                    except Exception as ex:
                        raise ConfigError(self._failed_to_load_model_error(path, ex), path)

        return models

    def _load_python_models(
        self,
        macros: MacroRegistry,
        jinja_macros: JinjaMacroRegistry,
        audits: UniqueKeyDict[str, ModelAudit],
        signals: UniqueKeyDict[str, signal],
    ) -> UniqueKeyDict[str, Model]:
        """Loads the python models into a Dict"""
        models: UniqueKeyDict[str, Model] = UniqueKeyDict("models")
        registry = model_registry.registry()
        registry.clear()
        registered: t.Set[str] = set()

        model_registry._dialect = self.config.model_defaults.dialect
        try:
            for path in self._glob_paths(
                self.config_path / c.MODELS,
                ignore_patterns=self.config.ignore_patterns,
                extension=".py",
            ):
                if not os.path.getsize(path):
                    continue

                self._track_file(path)
                try:
                    import_python_file(path, self.config_path)
                    new = registry.keys() - registered
                    registered |= new
                    for name in new:
                        for model in registry[name].models(
                            get_variables,
                            path=path,
                            module_path=self.config_path,
                            defaults=self.config.model_defaults.dict(),
                            macros=macros,
                            jinja_macros=jinja_macros,
                            dialect=self.config.model_defaults.dialect,
                            time_column_format=self.config.time_column_format,
                            physical_schema_mapping=self.config.physical_schema_mapping,
                            project=self.config.project,
                            default_catalog=self.context.default_catalog,
                            infer_names=self.config.model_naming.infer_names,
                            audit_definitions=audits,
                            signal_definitions=signals,
                            default_catalog_per_gateway=self.context.default_catalog_per_gateway,
                            virtual_environment_mode=self.config.virtual_environment_mode,
                        ):
                            if model.enabled:
                                models[model.fqn] = model
                except Exception as ex:
                    raise ConfigError(self._failed_to_load_model_error(path, ex), path)

        finally:
            model_registry._dialect = None

        return models

    def load_materializations(self) -> None:
        with sys_path(self.config_path):
            self._load_materializations()

    def _load_materializations(self) -> None:
        for path in self._glob_paths(
            self.config_path / c.MATERIALIZATIONS,
            ignore_patterns=self.config.ignore_patterns,
            extension=".py",
        ):
            if os.path.getsize(path):
                import_python_file(path, self.config_path)

    def _load_signals(self) -> UniqueKeyDict[str, signal]:
        """Loads signals for the built-in scheduler."""

        base_signals = signal.get_registry()

        signals_max_mtime: t.Optional[float] = None

        for path in self._glob_paths(
            self.config_path / c.SIGNALS,
            ignore_patterns=self.config.ignore_patterns,
            extension=".py",
        ):
            if os.path.getsize(path):
                self._track_file(path)
                signal_file_mtime = self._path_mtimes[path]
                signals_max_mtime = (
                    max(signals_max_mtime, signal_file_mtime)
                    if signals_max_mtime
                    else signal_file_mtime
                )
                import_python_file(path, self.config_path)

        self._signals_max_mtime = signals_max_mtime

        signals = signal.get_registry()
        signal.set_registry(base_signals)

        return signals

    def _load_audits(
        self, macros: MacroRegistry, jinja_macros: JinjaMacroRegistry
    ) -> UniqueKeyDict[str, Audit]:
        """Loads all the model audits."""
        audits_by_name: UniqueKeyDict[str, Audit] = UniqueKeyDict("audits")
        audits_max_mtime: t.Optional[float] = None
        variables = get_variables()

        for path in self._glob_paths(
            self.config_path / c.AUDITS,
            ignore_patterns=self.config.ignore_patterns,
            extension=".sql",
        ):
            self._track_file(path)
            with open(path, "r", encoding="utf-8") as file:
                audits_file_mtime = self._path_mtimes[path]
                audits_max_mtime = (
                    max(audits_max_mtime, audits_file_mtime)
                    if audits_max_mtime
                    else audits_file_mtime
                )
                expressions = parse(file.read(), default_dialect=self.config.model_defaults.dialect)
                audits = load_multiple_audits(
                    expressions=expressions,
                    path=path,
                    module_path=self.config_path,
                    macros=macros,
                    jinja_macros=jinja_macros,
                    dialect=self.config.model_defaults.dialect,
                    default_catalog=self.context.default_catalog,
                    variables=variables,
                    project=self.config.project,
                )
                for audit in audits:
                    audits_by_name[audit.name] = audit

        self._audits_max_mtime = audits_max_mtime

        return audits_by_name

    def _load_metrics(self) -> UniqueKeyDict[str, MetricMeta]:
        """Loads all metrics."""
        metrics: UniqueKeyDict[str, MetricMeta] = UniqueKeyDict("metrics")

        for path in self._glob_paths(
            self.config_path / c.METRICS,
            ignore_patterns=self.config.ignore_patterns,
            extension=".sql",
        ):
            if not os.path.getsize(path):
                continue
            self._track_file(path)

            with open(path, "r", encoding="utf-8") as file:
                dialect = self.config.model_defaults.dialect
                try:
                    for expression in parse(file.read(), default_dialect=dialect):
                        metric = load_metric_ddl(expression, path=path, dialect=dialect)
                        metrics[metric.name] = metric
                except SqlglotError as ex:
                    raise ConfigError(
                        f"Failed to parse metric definitions at '{path}': {ex}.", path
                    )

        return metrics

    def _load_environment_statements(self, macros: MacroRegistry) -> t.List[EnvironmentStatements]:
        """Loads environment statements."""

        if self.config.before_all or self.config.after_all:
            statements = {
                "before_all": self.config.before_all or [],
                "after_all": self.config.after_all or [],
            }
            dialect = self.config.model_defaults.dialect
            python_env = make_python_env(
                [
                    exp.maybe_parse(stmt, dialect=dialect)
                    for stmts in statements.values()
                    for stmt in stmts
                ],
                module_path=self.config_path,
                jinja_macro_references=None,
                macros=macros,
                variables=get_variables(),
                path=self.config_path,
            )

            return [
                EnvironmentStatements(
                    **statements, python_env=python_env, project=self.config.project or None
                )
            ]
        return []

    def _load_linting_rules(self) -> RuleSet:
        user_rules: UniqueKeyDict[str, type[Rule]] = UniqueKeyDict("rules")

        for path in self._glob_paths(
            self.config_path / c.LINTER,
            ignore_patterns=self.config.ignore_patterns,
            extension=".py",
        ):
            if os.path.getsize(path):
                self._track_file(path)
                module = import_python_file(path, self.config_path)
                _rule_exclude: t.Set[t.Type[Rule]] = {Rule}  # type: ignore[type-abstract]
                module_rules = subclasses(module.__name__, Rule, exclude=_rule_exclude)
                for user_rule in module_rules:
                    user_rules[user_rule.name] = user_rule

        return RuleSet(user_rules.values())

    def _load_model_test_file(self, path: Path) -> dict[str, ModelTestMetadata]:
        """Load a single model test file."""
        model_test_metadata = {}

        with open(path, "r", encoding="utf-8") as file:
            source = file.read()
            # If the user has specified a quoted/escaped gateway (e.g. "gateway: 'ma\tin'"), we need to
            # parse it as YAML to match the gateway name stored in the config
            gateway_line = GATEWAY_PATTERN.search(source)
            gateway = YAML().load(gateway_line.group(0))["gateway"] if gateway_line else None

        contents = yaml_load(source, variables=get_variables(gateway))

        for test_name, value in contents.items():
            model_test_metadata[test_name] = ModelTestMetadata(
                path=path, test_name=test_name, body=value
            )

        return model_test_metadata

    def load_model_tests(self) -> t.List[ModelTestMetadata]:
        """Loads YAML-based model tests"""
        test_meta_list: t.List[ModelTestMetadata] = []

        search_path = Path(self.config_path) / c.TESTS

        for yaml_file in itertools.chain(
            search_path.glob("**/test*.yaml"),
            search_path.glob("**/test*.yml"),
        ):
            if any(
                yaml_file.match(ignore_pattern)
                for ignore_pattern in self.config.ignore_patterns or []
            ):
                continue

            test_meta_list.extend(self._load_model_test_file(yaml_file).values())

        return test_meta_list

    class _Cache(CacheBase):
        def __init__(self, loader: SqlMeshLoader, config_path: Path):
            self._loader = loader
            self.config_path = config_path
            self._model_cache = ModelCache(self._loader.context.cache_dir)

        def get_or_load_models(
            self, target_path: Path, loader: t.Callable[[], t.List[Model]]
        ) -> t.List[Model]:
            models = self._model_cache.get_or_load(
                self._cache_entry_name(target_path),
                self._model_cache_entry_id(target_path),
                loader=loader,
            )

            for model in models:
                model._path = target_path

            return models

        def put(self, models: t.List[Model], path: Path) -> bool:
            return self._model_cache.put(
                models,
                self._cache_entry_name(path),
                self._model_cache_entry_id(path),
            )

        def get(self, path: Path) -> t.List[Model]:
            models = self._model_cache.get(
                self._cache_entry_name(path),
                self._model_cache_entry_id(path),
            )

            for model in models:
                model._path = path

            return models

        def _cache_entry_name(self, target_path: Path) -> str:
            return "__".join(target_path.relative_to(self.config_path).parts).replace(
                target_path.suffix, ""
            )

        def _model_cache_entry_id(self, model_path: Path) -> str:
            mtimes = [
                self._loader._path_mtimes[model_path],
                self._loader._macros_max_mtime,
                self._loader._signals_max_mtime,
                self._loader._audits_max_mtime,
                self._loader._config_mtimes.get(self.config_path),
                self._loader._config_mtimes.get(c.SQLMESH_PATH),
            ]
            return "__".join(
                [
                    str(max(m for m in mtimes if m is not None)),
                    self._loader.config.fingerprint,
                    # default catalog can change outside sqlmesh (e.g., DB user's
                    # default catalog), and it is retained in cached model's fully
                    # qualified name
                    self._loader.context.default_catalog or "",
                    # gateway is configurable, and it is retained in a cached
                    # model's python environment if the @gateway macro variable is
                    # used in the model
                    self._loader.context.gateway or self._loader.config.default_gateway_name,
                ]
            )
