from __future__ import annotations

import logging
import typing as t
from pathlib import Path

from sqlglot import exp
from sqlglot.helper import seq_get
from sqlglot.optimizer.simplify import gen
from sqlglot.schema import MappingSchema

from sqlmesh.core import constants as c
from sqlmesh.core.model.definition import ExternalModel, Model, SqlModel, _Model
from sqlmesh.utils.cache import FileCache
from sqlmesh.utils.hashing import crc32
from sqlmesh.utils.process import PoolExecutor, create_process_pool_executor

from dataclasses import dataclass

logger = logging.getLogger(__name__)

if t.TYPE_CHECKING:
    from sqlmesh.core.snapshot import SnapshotId
    from sqlmesh.core.linter.rule import Rule

    T = t.TypeVar("T")


class ModelCache:
    """File-based cache implementation for model definitions.

    Args:
        path: The path to the cache folder.
    """

    def __init__(self, path: Path):
        self.path = path
        self._file_cache: FileCache[t.List[Model]] = FileCache(
            path,
            prefix="model_definition",
        )

    def get_or_load(
        self, name: str, entry_id: str = "", *, loader: t.Callable[[], t.List[Model]]
    ) -> t.List[Model]:
        """Returns an existing cached model definition or loads and caches a new one.
        Args:
            name: The name of the entry.
            entry_id: The unique entry identifier. Used for cache invalidation.
            loader: Used to load a new model definition when no cached instance was found.
        Returns:
            The model definition.
        """
        cache_entry = self._file_cache.get(name, entry_id)
        if isinstance(cache_entry, list) and isinstance(seq_get(cache_entry, 0), _Model):
            return cache_entry

        models = loader()
        if isinstance(models, list) and isinstance(seq_get(models, 0), (SqlModel, ExternalModel)):
            # make sure we preload full_depends_on
            for model in models:
                model.full_depends_on

            self._file_cache.put(name, entry_id, value=models)
        return models

    def put(self, models: t.List[Model], name: str, entry_id: str = "") -> bool:
        if models and isinstance(seq_get(models, 0), (SqlModel, ExternalModel)):
            # make sure we preload full_depends_on
            for model in models:
                model.full_depends_on

            self._file_cache.put(name, entry_id, value=models)
            return True

        return False

    def get(self, name: str, entry_id: str = "") -> t.List[Model]:
        return self._file_cache.get(name, entry_id) or []


@dataclass
class OptimizedQueryCacheEntry:
    optimized_rendered_query: t.Optional[exp.Query]
    renderer_violations: t.Optional[t.Dict[type[Rule], t.Any]]


class OptimizedQueryCache:
    """File-based cache implementation for optimized model queries.

    Args:
        path: The path to the cache folder.
    """

    def __init__(self, path: Path):
        self.path = path
        self._file_cache: FileCache[OptimizedQueryCacheEntry] = FileCache(
            path, prefix="optimized_query"
        )

    def with_optimized_query(self, model: Model, name: t.Optional[str] = None) -> bool:
        """Adds an optimized query to the model's in-memory cache.

        Args:
            model: The model to add the optimized query to.
            name: The cache entry name of the model.
        """
        if not isinstance(model, SqlModel):
            return False

        name = self._entry_name(model) if name is None else name
        cache_entry = self._file_cache.get(name)
        if cache_entry:
            try:
                # If the optimized rendered query is None, then there are likely adapter calls in the query
                # that prevent us from rendering it at load time. This means that we can safely set the
                # unoptimized cache to None as well to prevent attempts to render it downstream.
                optimized = cache_entry.optimized_rendered_query is not None
                model._query_renderer.update_cache(
                    cache_entry.optimized_rendered_query,
                    cache_entry.renderer_violations,
                    optimized=optimized,
                )
                return True
            except Exception as ex:
                logger.warning("Failed to load a cache entry '%s': %s", name, ex)

        self._put(name, model)
        return False

    def put(self, model: Model) -> t.Optional[str]:
        if not isinstance(model, SqlModel):
            return None

        name = self._entry_name(model)

        if self._file_cache.exists(name):
            return name

        self._put(name, model)
        return name

    def _put(self, name: str, model: SqlModel) -> None:
        optimized_query = model.render_query()

        new_entry = OptimizedQueryCacheEntry(
            optimized_rendered_query=optimized_query,
            renderer_violations=model.violated_rules_for_query,
        )
        self._file_cache.put(name, value=new_entry)

    @staticmethod
    def _entry_name(model: SqlModel) -> str:
        hash_data = _mapping_schema_hash_data(model.mapping_schema)
        hash_data.append(gen(model.query, comments=True))
        hash_data.append(str([gen(d) for d in model.macro_definitions]))
        hash_data.append(str([(k, v) for k, v in model.sorted_python_env]))
        hash_data.extend(model.jinja_macros.data_hash_values)
        return f"{model.name}_{crc32(hash_data)}"


def optimized_query_cache_pool(optimized_query_cache: OptimizedQueryCache) -> PoolExecutor:
    return create_process_pool_executor(
        initializer=_init_optimized_query_cache,
        initargs=(optimized_query_cache,),
        max_workers=c.MAX_FORK_WORKERS,
    )


_optimized_query_cache: t.Optional[OptimizedQueryCache] = None


def _init_optimized_query_cache(optimized_query_cache: OptimizedQueryCache) -> None:
    global _optimized_query_cache
    _optimized_query_cache = optimized_query_cache


def load_optimized_query(
    model_snapshot_id: t.Tuple[Model, SnapshotId],
) -> t.Tuple[SnapshotId, t.Optional[str]]:
    assert _optimized_query_cache
    model, snapshot_id = model_snapshot_id

    entry_name = None

    if isinstance(model, SqlModel):
        try:
            entry_name = _optimized_query_cache.put(model)
        except:
            # this can happen if there is a query rendering error.
            # for example, the model query references some python library or function that was available
            # at the time the model was created but has since been removed locally
            logger.exception(f"Failed to cache optimized query for model '{model.name}'")

    return snapshot_id, entry_name


def load_optimized_query_and_mapping(
    model: Model, mapping: t.Dict
) -> t.Tuple[str, t.Optional[str], str, str, t.Dict]:
    assert _optimized_query_cache

    schema = MappingSchema(normalize=False)
    for parent, columns_to_types in mapping.items():
        schema.add_table(parent, columns_to_types, dialect=model.dialect)
    model.update_schema(schema)

    if isinstance(model, SqlModel):
        entry_name = _optimized_query_cache._entry_name(model)
        _optimized_query_cache.with_optimized_query(model, entry_name)
    else:
        entry_name = None

    return (
        model.fqn,
        entry_name,
        model.data_hash,
        model.metadata_hash,
        model.mapping_schema,
    )


def _mapping_schema_hash_data(schema: t.Dict[str, t.Any]) -> t.List[str]:
    keys = sorted(schema) if all(isinstance(v, dict) for v in schema.values()) else schema

    data = []
    for k in keys:
        data.append(k)
        if isinstance(schema[k], dict):
            data.extend(_mapping_schema_hash_data(schema[k]))
        else:
            data.append(str(schema[k]))

    return data
