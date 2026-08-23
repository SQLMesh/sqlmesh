# ruff: noqa: E402
from __future__ import annotations

import json
import logging
import os
import re
import typing as t
from argparse import Namespace
from collections import defaultdict
from functools import cached_property
from pathlib import Path

from dbt import flags

from sqlmesh.dbt.util import DBT_VERSION
from sqlmesh.utils.conversions import make_serializable

# Override the file name to prevent dbt commands from invalidating the cache.

if DBT_VERSION >= (1, 6, 0):
    from dbt import constants as dbt_constants

    dbt_constants.PARTIAL_PARSE_FILE_NAME = "sqlmesh_partial_parse.msgpack"  # type: ignore
else:
    from dbt.parser import manifest as dbt_manifest  # type: ignore

    dbt_manifest.PARTIAL_PARSE_FILE_NAME = "sqlmesh_partial_parse.msgpack"  # type: ignore

import jinja2
from dbt.adapters.factory import register_adapter, reset_adapters
from dbt.config import Profile, Project, RuntimeConfig
from dbt.config.profile import read_profile
from dbt.config.renderer import DbtProjectYamlRenderer, ProfileRenderer
from dbt.parser.manifest import ManifestLoader

try:
    from dbt.parser.sources import merge_freshness  # type: ignore[attr-defined]
except ImportError:
    # merge_freshness was renamed to merge_source_freshness in dbt 1.10
    # ref: https://github.com/dbt-labs/dbt-core/commit/14fc39a76ff4830cdf2fcbe73f57ca27db500018#diff-1f09db95588f46879a83378c2a86d6b16b7cdfcaddbfe46afc5d919ee5e9a4d9R430
    from dbt.parser.sources import merge_source_freshness as merge_freshness  # type: ignore[no-redef,attr-defined]

from dbt.tracking import do_not_track

from sqlmesh.core import constants as c
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.core.config import ModelDefaultsConfig
from sqlmesh.dbt.builtin import BUILTIN_FILTERS, BUILTIN_GLOBALS, OVERRIDDEN_MACROS
from sqlmesh.dbt.common import Dependencies
from sqlmesh.dbt.model import ModelConfig
from sqlmesh.dbt.package import HookConfig, MacroConfig, MaterializationConfig
from sqlmesh.dbt.seed import SeedConfig
from sqlmesh.dbt.source import SourceConfig
from sqlmesh.dbt.target import TargetConfig
from sqlmesh.dbt.test import TestConfig
from sqlmesh.dbt.util import DBT_VERSION
from sqlmesh.utils.cache import FileCache
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.jinja import (
    MacroInfo,
    MacroReference,
    extract_call_names,
    jinja_call_arg_name,
)
from sqlglot.helper import ensure_list

if t.TYPE_CHECKING:
    from dbt.contracts.graph.manifest import Macro, Manifest
    from dbt.contracts.graph.nodes import ManifestNode, SourceDefinition
    from sqlmesh.utils.jinja import CallNames

logger = logging.getLogger(__name__)

TestConfigs = t.Dict[str, TestConfig]
ModelConfigs = t.Dict[str, ModelConfig]
SeedConfigs = t.Dict[str, SeedConfig]
SourceConfigs = t.Dict[str, SourceConfig]
MacroConfigs = t.Dict[str, MacroConfig]
HookConfigs = t.Dict[str, HookConfig]
MaterializationConfigs = t.Dict[str, MaterializationConfig]


IGNORED_PACKAGES = {"elementary"}
BUILTIN_CALLS = {*BUILTIN_GLOBALS, *BUILTIN_FILTERS}

# Patch Semantic Manifest to skip validation and avoid Pydantic v1 errors on DBT 1.6
# We patch for 1.7+ since we don't care about semantic models
if DBT_VERSION >= (1, 6, 0):
    from dbt.contracts.graph.semantic_manifest import SemanticManifest  # type: ignore

    SemanticManifest.validate = lambda _: True  # type: ignore


class ManifestHelper:
    def __init__(
        self,
        project_path: Path,
        profiles_path: Path,
        profile_name: str,
        target: TargetConfig,
        variable_overrides: t.Optional[t.Dict[str, t.Any]] = None,
        cache_dir: t.Optional[str] = None,
        model_defaults: t.Optional[ModelDefaultsConfig] = None,
    ):
        self.project_path = project_path
        self.profiles_path = profiles_path
        self.profile_name = profile_name
        self.target = target
        self.variable_overrides = variable_overrides or {}
        self.model_defaults = model_defaults or ModelDefaultsConfig()

        self.__manifest: t.Optional[Manifest] = None
        self._project_name: str = ""

        self._is_loaded: bool = False
        self._tests_per_package: t.Dict[str, TestConfigs] = defaultdict(dict)
        self._models_per_package: t.Dict[str, ModelConfigs] = defaultdict(dict)
        self._seeds_per_package: t.Dict[str, SeedConfigs] = defaultdict(dict)
        self._sources_per_package: t.Dict[str, SourceConfigs] = defaultdict(dict)
        self._macros_per_package: t.Dict[str, MacroConfigs] = defaultdict(dict)

        self._macro_flatten_dependencies: t.Dict[str, t.Dict[str, Dependencies]] = defaultdict(dict)

        self._tests_by_owner: t.Dict[str, t.List[TestConfig]] = defaultdict(list)
        self._disabled_refs: t.Optional[t.Set[str]] = None
        self._disabled_sources: t.Optional[t.Set[str]] = None

        if cache_dir is not None:
            cache_path = Path(cache_dir)
            if not cache_path.is_absolute():
                cache_path = self.project_path / cache_path
        else:
            cache_path = self.project_path / c.CACHE

        self._call_cache: FileCache[t.Dict[str, t.List[CallNames]]] = FileCache(
            cache_path, "jinja_calls"
        )

        self._on_run_start_per_package: t.Dict[str, HookConfigs] = defaultdict(dict)
        self._on_run_end_per_package: t.Dict[str, HookConfigs] = defaultdict(dict)
        self._materializations: MaterializationConfigs = {}

    def tests(self, package_name: t.Optional[str] = None) -> TestConfigs:
        self._load_all()
        return self._tests_per_package[package_name or self._project_name]

    def models(self, package_name: t.Optional[str] = None) -> ModelConfigs:
        self._load_all()
        return self._models_per_package[package_name or self._project_name]

    def seeds(self, package_name: t.Optional[str] = None) -> SeedConfigs:
        self._load_all()
        return self._seeds_per_package[package_name or self._project_name]

    def sources(self, package_name: t.Optional[str] = None) -> SourceConfigs:
        self._load_all()
        return self._sources_per_package[package_name or self._project_name]

    def macros(self, package_name: t.Optional[str] = None) -> MacroConfigs:
        self._load_all()
        return self._macros_per_package[package_name or self._project_name]

    def on_run_start(self, package_name: t.Optional[str] = None) -> HookConfigs:
        self._load_all()
        return self._on_run_start_per_package[package_name or self._project_name]

    def on_run_end(self, package_name: t.Optional[str] = None) -> HookConfigs:
        self._load_all()
        return self._on_run_end_per_package[package_name or self._project_name]

    def materializations(self) -> MaterializationConfigs:
        self._load_all()
        return self._materializations

    @property
    def all_macros(self) -> t.Dict[str, t.Dict[str, MacroInfo]]:
        self._load_all()
        result: t.Dict[str, t.Dict[str, MacroInfo]] = defaultdict(dict)
        for package_name, macro_configs in self._macros_per_package.items():
            for macro_name, macro_config in macro_configs.items():
                result[package_name][macro_name] = macro_config.info
        return result

    @cached_property
    def flat_graph(self) -> t.Dict[str, t.Any]:
        return {
            "exposures": {
                k: make_serializable(v.to_dict(omit_none=False))
                for k, v in getattr(self._manifest, "exposures", {}).items()
            },
            "groups": {
                k: make_serializable(v.to_dict(omit_none=False))
                for k, v in getattr(self._manifest, "groups", {}).items()
            },
            "metrics": {
                k: make_serializable(v.to_dict(omit_none=False))
                for k, v in getattr(self._manifest, "metrics", {}).items()
            },
            "nodes": {
                k: make_serializable(v.to_dict(omit_none=False))
                for k, v in self._manifest.nodes.items()
            },
            "sources": {
                k: make_serializable(v.to_dict(omit_none=False))
                for k, v in self._manifest.sources.items()
            },
            "semantic_models": {
                k: make_serializable(v.to_dict(omit_none=False))
                for k, v in getattr(self._manifest, "semantic_models", {}).items()
            },
            "saved_queries": {
                k: make_serializable(v.to_dict(omit_none=False))
                for k, v in getattr(self._manifest, "saved_queries", {}).items()
            },
        }

    def _load_all(self) -> None:
        if self._is_loaded:
            return

        self._calls = {k: (v, False) for k, v in (self._call_cache.get("") or {}).items()}

        self._load_macros()
        self._load_materializations()
        self._load_sources()
        self._load_tests()
        self._load_models_and_seeds()
        self._load_on_run_start_end()
        self._is_loaded = True

        self._call_cache.put("", value={k: v for k, (v, used) in self._calls.items() if used})

    def _load_sources(self) -> None:
        for source in self._manifest.sources.values():
            # starting in dbt-core 1.9.5, freshness can be set in both source and source config
            source_dict = source.to_dict()
            source_dict.pop("freshness", None)

            source_config_dict = _config(source)
            source_config_dict.pop("freshness", None)

            source_config_freshness = getattr(source.config, "freshness", None)
            freshness = (
                merge_freshness(source.freshness, source_config_freshness)
                if source_config_freshness
                else source.freshness
            )

            source_config = SourceConfig(
                **{
                    **source_dict,
                    **source_config_dict,
                    "freshness": freshness.to_dict() if freshness else None,
                }
            )
            self._sources_per_package[source.package_name][source_config.config_name] = (
                source_config
            )

    def _load_macros(self) -> None:
        for macro in self._manifest.macros.values():
            if macro.name.startswith("materialization_"):
                continue

            if macro.name.startswith("test_"):
                macro.macro_sql = _convert_jinja_test_to_macro(macro.macro_sql)

            dependencies = Dependencies(macros=_macro_references(self._manifest, macro))
            if not macro.name.startswith("test_"):
                dependencies = dependencies.union(
                    self._extra_dependencies(macro.macro_sql, macro.package_name)
                )

            self._macros_per_package[macro.package_name][macro.name] = MacroConfig(
                info=MacroInfo(
                    definition=macro.macro_sql,
                    depends_on=dependencies.macros,
                ),
                dependencies=dependencies,
                path=Path(macro.original_file_path),
            )

        # This is a workaround for dbt adapter macros (eg. "spark__dateadd") whcih are expected to be
        # available in the global scope regardless of the package they came from.
        adapter_macro_names = {
            name[name.find("__") + 2 :]
            for name in self._macros_per_package.get("dbt", {})
            if "__" in name
        }
        for macros in self._macros_per_package.values():
            for name, macro_config in macros.items():
                pos = name.find("__")
                if pos > 0 and name[pos + 2 :] in adapter_macro_names:
                    macro_config.info.is_top_level = True

    def _load_materializations(self) -> None:
        for macro in self._manifest.macros.values():
            if macro.name.startswith("materialization_"):
                # Extract name and adapter ( "materialization_{name}_{adapter}" or "materialization_{name}_default")
                name_parts = macro.name.split("_")
                if len(name_parts) >= 3:
                    mat_name = "_".join(name_parts[1:-1])
                    adapter = name_parts[-1]

                    dependencies = Dependencies(macros=_macro_references(self._manifest, macro))
                    macro.macro_sql = _strip_jinja_materialization_tags(macro.macro_sql)
                    dependencies = dependencies.union(
                        self._extra_dependencies(macro.macro_sql, macro.package_name)
                    )

                    materialization_config = MaterializationConfig(
                        name=mat_name,
                        adapter=adapter,
                        definition=macro.macro_sql,
                        dependencies=dependencies,
                        path=Path(macro.original_file_path),
                    )

                    key = f"{mat_name}_{adapter}"
                    self._materializations[key] = materialization_config

    def _load_tests(self) -> None:
        for node in self._manifest.nodes.values():
            if node.resource_type != "test":
                continue

            skip_test = False
            refs = _refs(node)
            for ref in refs:
                if self._is_disabled_ref(ref):
                    logger.info(
                        "Skipping test '%s' which references a disabled model '%s'",
                        node.name,
                        ref,
                    )
                    skip_test = True
                    break

            if skip_test:
                continue

            dependencies = Dependencies(
                macros=_macro_references(self._manifest, node),
                refs=refs,
                sources=_sources(node),
            )
            # Implicit dependencies for model test arg
            dependencies.macros.append(MacroReference(package="dbt", name="get_where_subquery"))
            dependencies.macros.append(MacroReference(package="dbt", name="should_store_failures"))

            sql = node.raw_code if DBT_VERSION >= (1, 3, 0) else node.raw_sql  # type: ignore
            dependencies = dependencies.union(self._extra_dependencies(sql, node.package_name))
            dependencies = dependencies.union(
                self._flatten_dependencies_from_macros(dependencies.macros, node.package_name)
            )

            test_model = _test_model(node)
            node_config = _node_base_config(node)
            node_config["name"] = _build_test_name(node, dependencies)

            test = TestConfig(
                sql=sql,
                model_name=test_model,
                test_kwargs=node.test_metadata.kwargs if hasattr(node, "test_metadata") else {},
                dependencies=dependencies,
                **node_config,
            )
            self._tests_per_package[node.package_name][node.unique_id] = test
            if test_model:
                self._tests_by_owner[test_model].append(test)

    def _load_models_and_seeds(self) -> None:
        for node in self._manifest.nodes.values():
            if (
                node.resource_type not in ("model", "seed", "snapshot")
                or node.package_name in IGNORED_PACKAGES
            ):
                continue

            macro_references = _macro_references(self._manifest, node)
            all_tests = (
                self._tests_by_owner[node.name]
                + self._tests_by_owner[f"{node.package_name}.{node.name}"]
            )
            # Only include non-standalone tests (tests that don't reference other models)
            tests = [test for test in all_tests if not test.is_standalone]
            node_config = _node_base_config(node)

            node_name = node.name
            node_version = getattr(node, "version", None)
            if node_version:
                node_name = f"{node_name}_v{node_version}"

            if node.resource_type in {"model", "snapshot"}:
                sql = node.raw_code if DBT_VERSION >= (1, 3, 0) else node.raw_sql  # type: ignore
                dependencies = Dependencies(
                    macros=macro_references, refs=_refs(node), sources=_sources(node)
                )
                dependencies = dependencies.union(
                    self._extra_dependencies(sql, node.package_name, track_all_model_attrs=True)
                )
                for hook in [*node_config.get("pre-hook", []), *node_config.get("post-hook", [])]:
                    dependencies = dependencies.union(
                        self._extra_dependencies(
                            hook["sql"], node.package_name, track_all_model_attrs=True
                        )
                    )
                dependencies = dependencies.union(
                    self._flatten_dependencies_from_macros(dependencies.macros, node.package_name)
                )

                self._models_per_package[node.package_name][node_name] = ModelConfig(
                    **dict(
                        node_config,
                        sql=sql,
                        dependencies=dependencies,
                        tests=tests,
                    )
                )
            else:
                self._seeds_per_package[node.package_name][node_name] = SeedConfig(
                    **dict(
                        node_config,
                        dependencies=Dependencies(macros=macro_references),
                        tests=tests,
                    )
                )

    def _load_on_run_start_end(self) -> None:
        for node in self._manifest.nodes.values():
            if node.resource_type == "operation" and (
                set(node.tags) & {"on-run-start", "on-run-end"}
            ):
                sql = node.raw_code if DBT_VERSION >= (1, 3, 0) else node.raw_sql  # type: ignore
                node_name = node.name
                node_path = Path(node.original_file_path)

                dependencies = Dependencies(
                    macros=_macro_references(self._manifest, node),
                    refs=_refs(node),
                    sources=_sources(node),
                )
                dependencies = dependencies.union(self._extra_dependencies(sql, node.package_name))
                dependencies = dependencies.union(
                    self._flatten_dependencies_from_macros(dependencies.macros, node.package_name)
                )

                if "on-run-start" in node.tags:
                    self._on_run_start_per_package[node.package_name][node_name] = HookConfig(
                        sql=sql,
                        index=getattr(node, "index", None) or 0,
                        path=node_path,
                        dependencies=dependencies,
                    )
                else:
                    self._on_run_end_per_package[node.package_name][node_name] = HookConfig(
                        sql=sql,
                        index=getattr(node, "index", None) or 0,
                        path=node_path,
                        dependencies=dependencies,
                    )

    @property
    def _manifest(self) -> Manifest:
        if not self.__manifest:
            try:
                self.__manifest = self._load_manifest()
            except Exception as ex:
                raise SQLMeshError(f"Failed to load dbt manifest: {ex}") from ex
        return self.__manifest

    def _load_manifest(self) -> Manifest:
        do_not_track()

        variables = (
            self.variable_overrides
            if DBT_VERSION >= (1, 5, 0)
            else json.dumps(self.variable_overrides)
        )

        args: Namespace = Namespace(
            vars=variables,
            profile=self.profile_name,
            project_dir=str(self.project_path),
            profiles_dir=str(self.profiles_path),
            target=self.target.name,
            macro_debugging=False,
            REQUIRE_RESOURCE_NAMES_WITHOUT_SPACES=True,
        )
        flags.set_from_args(args, None)

        if DBT_VERSION >= (1, 8, 0):
            from dbt_common.context import set_invocation_context  # type: ignore

            set_invocation_context(os.environ)

        profile = self._load_profile()
        project = self._load_project(profile)

        if (
            not any(k in project.models for k in ("start", "+start"))
            and not self.model_defaults.start
        ):
            raise ConfigError(
                "SQLMesh requires a start date in order to have a finite range of backfilling data. Add start to the 'models:' block in dbt_project.yml. https://sqlmesh.readthedocs.io/en/stable/integrations/dbt/#setting-model-backfill-start-dates"
            )

        runtime_config = RuntimeConfig.from_parts(project, profile, args)

        self._project_name = project.project_name

        if DBT_VERSION >= (1, 8, 0):
            from dbt.mp_context import get_mp_context  # type: ignore

            register_adapter(runtime_config, get_mp_context())  # type: ignore
        else:
            register_adapter(runtime_config)  # type: ignore

        manifest = ManifestLoader.get_full_manifest(runtime_config)
        # This adapter doesn't care about semantic models so we clear them out to avoid issues
        manifest.semantic_models = {}
        reset_adapters()
        return manifest

    def _load_project(self, profile: Profile) -> Project:
        project_renderer = DbtProjectYamlRenderer(profile, cli_vars=self.variable_overrides)
        return Project.from_project_root(str(self.project_path), project_renderer)

    def _load_profile(self) -> Profile:
        profile_renderer = ProfileRenderer(cli_vars=self.variable_overrides)
        raw_profiles = read_profile(str(self.profiles_path))
        return Profile.from_raw_profiles(
            raw_profiles=raw_profiles,
            profile_name=self.profile_name,
            renderer=profile_renderer,
            target_override=self.target.name,
        )

    def _is_disabled_ref(self, ref: str) -> bool:
        if self._disabled_refs is None:
            self._load_disabled()

        return ref in self._disabled_refs  # type: ignore

    def _is_disabled_source(self, source: str) -> bool:
        if self._disabled_sources is None:
            self._load_disabled()

        return source in self._disabled_sources  # type: ignore

    def _load_disabled(self) -> None:
        self._disabled_refs = set()
        self._disabled_sources = set()
        for nodes in self._manifest.disabled.values():
            for node in nodes:
                if node.resource_type in ("model", "snapshot", "seed"):
                    self._disabled_refs.add(f"{node.package_name}.{node.name}")
                    self._disabled_refs.add(node.name)
                elif node.resource_type == "source":
                    self._disabled_sources.add(f"{node.package_name}.{node.name}")

        for node in self._manifest.nodes.values():
            if node.resource_type in ("model", "snapshot", "seed"):
                self._disabled_refs.discard(node.name)
            elif node.resource_type == "source":
                self._disabled_sources.discard(node.name)

    def _flatten_dependencies_from_macros(
        self,
        macros: t.List[MacroReference],
        default_package: str,
        visited: t.Optional[t.Set[t.Tuple[str, str]]] = None,
    ) -> Dependencies:
        if visited is None:
            visited = set()

        dependencies = Dependencies()
        for macro in macros:
            macro_package = macro.package or default_package

            if (macro_package, macro.name) in visited:
                continue
            visited.add((macro_package, macro.name))

            macro_dependencies = self._macro_flatten_dependencies.get(macro_package, {}).get(
                macro.name
            )
            if not macro_dependencies:
                macro_config = self._macros_per_package[macro_package].get(macro.name)
                if not macro_config:
                    continue

                macro_dependencies = macro_config.dependencies.union(
                    self._flatten_dependencies_from_macros(
                        macro_config.dependencies.macros, macro_package, visited=visited
                    )
                )
                # We don't need flatten macro dependencies. The jinja macro registry takes care of recursive
                # dependencies for us.
                macro_dependencies.macros = []
                self._macro_flatten_dependencies[macro_package][macro.name] = macro_dependencies
            dependencies = dependencies.union(macro_dependencies)
        return dependencies

    def _extra_dependencies(
        self,
        target: str,
        package: str,
        track_all_model_attrs: bool = False,
    ) -> Dependencies:
        """
        We sometimes observe that the manifest doesn't capture all macros, refs, and sources within a macro.
        This behavior has been observed with macros like dbt.current_timestamp(), dbt_utils.slugify(), and source().
        Here we apply our custom extractor to make a best effort to supplement references captured in the manifest.
        """
        dependencies = Dependencies()

        # Whether all `model` attributes (e.g., `model.config`) should be included in the dependencies
        all_model_attrs = False

        for call_name, node in extract_call_names(target, cache=self._calls):
            if call_name[0] == "config":
                continue

            if (
                track_all_model_attrs
                and not all_model_attrs
                and isinstance(node, jinja2.nodes.Call)
                and any(isinstance(a, jinja2.nodes.Name) and a.name == "model" for a in node.args)
            ):
                all_model_attrs = True

            if isinstance(node, jinja2.nodes.Getattr):
                if call_name[0] == "model":
                    dependencies.model_attrs.attrs.add(call_name[1])
            elif call_name[0] == "source":
                args = [jinja_call_arg_name(arg) for arg in node.args]
                if args and all(arg for arg in args):
                    source = ".".join(args)
                    if not self._is_disabled_source(source):
                        dependencies.sources.add(source)
                dependencies.macros.append(MacroReference(name="source"))
            elif call_name[0] == "ref":
                args = [jinja_call_arg_name(arg) for arg in node.args]
                if args and all(arg for arg in args):
                    ref = ".".join(args)
                    if not self._is_disabled_ref(ref):
                        dependencies.refs.add(ref)
                dependencies.macros.append(MacroReference(name="ref"))
            elif call_name[0] == "var":
                args = [jinja_call_arg_name(arg) for arg in node.args]
                if args and args[0]:
                    dependencies.variables.add(args[0])
                else:
                    # We couldn't determine the var name statically
                    dependencies.has_dynamic_var_names = True
                dependencies.macros.append(MacroReference(name="var"))
            elif len(call_name) == 1:
                macro_name = call_name[0]
                if macro_name in BUILTIN_CALLS:
                    continue
                if (
                    f"macro.{package}.{macro_name}" not in self._manifest.macros
                    and f"macro.dbt.{macro_name}" in self._manifest.macros
                ):
                    package_name: t.Optional[str] = "dbt"
                else:
                    # dbt doesn't include the package name for project macros
                    package_name = package if package != self._project_name else None
                _macro_reference_if_not_overridden(
                    package_name, macro_name, dependencies.macros.append
                )
            else:
                if call_name[0] != "adapter":
                    _macro_reference_if_not_overridden(
                        call_name[0], call_name[1], dependencies.macros.append
                    )

        # When `model` is referenced as-is, e.g. it's passed as an argument to a macro call like
        # `{{ foo(model) }}`, we can't easily track the attributes that are actually used, because
        # it may be aliased and hence tracking actual uses of `model` requires a proper data flow
        # analysis. We conservatively deal with this by including all of its supported attributes
        # if a standalone reference is found.
        if all_model_attrs:
            dependencies.model_attrs.all_attrs = True

        return dependencies


def _macro_reference_if_not_overridden(
    package: t.Optional[str], name: str, if_not_overridden: t.Callable[[MacroReference], None]
) -> None:
    reference = MacroReference(package=package, name=name)
    if reference not in OVERRIDDEN_MACROS:
        if_not_overridden(reference)


def _config(node: t.Union[ManifestNode, SourceDefinition]) -> t.Dict[str, t.Any]:
    return node.config.to_dict()


def _macro_references(
    manifest: Manifest, node: t.Union[ManifestNode, Macro]
) -> t.Set[MacroReference]:
    result: t.Set[MacroReference] = set()
    if not hasattr(node, "depends_on"):
        return result

    for macro_node_id in node.depends_on.macros:
        if not macro_node_id or macro_node_id == "None":
            continue

        macro_node = manifest.macros[macro_node_id]
        macro_name = macro_node.name
        macro_package = (
            macro_node.package_name if macro_node.package_name != node.package_name else None
        )
        _macro_reference_if_not_overridden(macro_package, macro_name, result.add)
    return result


def _refs(node: ManifestNode) -> t.Set[str]:
    if DBT_VERSION >= (1, 5, 0):
        result: t.Set[str] = set()
        if not hasattr(node, "refs"):
            return result
        for r in node.refs:
            ref_name = f"{r.package}.{r.name}" if r.package else r.name  # type: ignore
            if getattr(r, "version", None):
                ref_name = f"{ref_name}_v{r.version}"  # type: ignore
            result.add(ref_name)
        return result
    return {".".join(r) for r in node.refs}  # type: ignore


def _sources(node: ManifestNode) -> t.Set[str]:
    return {".".join(s) for s in getattr(node, "sources", [])}


def _model_node_id(model_name: str, package: str) -> str:
    return f"model.{package}.{model_name}"


def _test_model(node: ManifestNode) -> t.Optional[str]:
    attached_node = getattr(node, "attached_node", None)
    if attached_node:
        pieces = attached_node.split(".")
        if pieces[0] in ["model", "seed"]:
            # versioned models have format "model.package.model_name.v1" (4 parts)
            if len(pieces) == 4:
                return f"{pieces[2]}_{pieces[3]}"
            return pieces[-1]
        return None

    key_name = getattr(node, "file_key_name", None)
    if key_name:
        pieces = key_name.split(".")
        return pieces[-1] if pieces[0] in ["models", "seeds"] else None

    return None


def _node_base_config(node: ManifestNode) -> t.Dict[str, t.Any]:
    return {
        **_config(node),
        **node.to_dict(),
        "path": Path(node.original_file_path),
    }


def _convert_jinja_test_to_macro(test_jinja: str) -> str:
    TEST_TAG_REGEX = r"\s*{%-?\s*test\s+"
    ENDTEST_REGEX = r"{%-?\s*endtest\s*-?%}"

    match = re.match(TEST_TAG_REGEX, test_jinja)
    if not match:
        # already a macro
        return test_jinja

    test_tag = test_jinja[: match.span()[-1]]

    macro_tag = re.sub(r"({%-?\s*)test\s+", r"\1macro test_", test_tag)
    macro = macro_tag + test_jinja[match.span()[-1] :]

    return re.sub(ENDTEST_REGEX, lambda m: m.group(0).replace("endtest", "endmacro"), macro)


def _strip_jinja_materialization_tags(materialization_jinja: str) -> str:
    MATERIALIZATION_TAG_REGEX = r"\s*{%-?\s*materialization\s+[^%]*%}\s*\n?"
    ENDMATERIALIZATION_REGEX = r"{%-?\s*endmaterialization\s*-?%}\s*\n?"

    if not re.match(MATERIALIZATION_TAG_REGEX, materialization_jinja):
        return materialization_jinja

    materialization_jinja = re.sub(
        MATERIALIZATION_TAG_REGEX,
        "",
        materialization_jinja,
        flags=re.IGNORECASE,
    )

    materialization_jinja = re.sub(
        ENDMATERIALIZATION_REGEX,
        "",
        materialization_jinja,
        flags=re.IGNORECASE,
    )

    return materialization_jinja.strip()


def _build_test_name(node: ManifestNode, dependencies: Dependencies) -> str:
    """
    Build a user-friendly test name that includes the test's model/source, column,
    and args for tests with custom user names. Needed because dbt only generates these
    names for tests that do not specify the "name" field in their YAML definition.

    Name structure
    - Model test:  [namespace]_[test name]_[model name]_[column name]__[arg values]
    - Source test: [namespace]_source_[test name]_[source name]_[table name]_[column name]__[arg values]
    """
    # standalone test
    if not hasattr(node, "test_metadata"):
        return node.name

    model_name = _test_model(node)
    source_name = None
    if not model_name and dependencies.sources:
        # extract source and table names
        source_parts = list(dependencies.sources)[0].split(".")
        source_name = "_".join(source_parts) if len(source_parts) == 2 else source_parts[-1]
    entity_name = model_name or source_name or ""
    entity_name = f"_{entity_name}" if entity_name else ""

    name_prefix = ""
    if namespace := getattr(node.test_metadata, "namespace", None):
        name_prefix += f"{namespace}_"
    if source_name and not model_name:
        name_prefix += "source_"

    metadata_kwargs = node.test_metadata.kwargs
    arg_val_parts = []
    for arg, val in sorted(metadata_kwargs.items()):
        if arg == "model":
            continue
        if isinstance(val, dict):
            val = list(val.values())
        items: t.List[t.Any] = ensure_list(val)
        val = [re.sub("[^0-9a-zA-Z_]+", "_", str(v)) for v in items]
        arg_val_parts.extend(val)
    unique_args = "__".join(arg_val_parts) if arg_val_parts else ""
    unique_args = f"_{unique_args}" if unique_args else ""

    auto_name = f"{name_prefix}{node.test_metadata.name}{entity_name}{unique_args}"

    if node.name == auto_name:
        return node.name

    custom_prefix = name_prefix if source_name and not model_name else ""
    return f"{custom_prefix}{node.name}{entity_name}{unique_args}"
