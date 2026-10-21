from __future__ import annotations

import typing as t
from concurrent.futures import as_completed
from pathlib import Path

from sqlglot.errors import SchemaError
from sqlglot.schema import MappingSchema

from sqlmesh.core.model.cache import (
    load_optimized_query_and_mapping,
    optimized_query_cache_pool,
    OptimizedQueryCache,
)

if t.TYPE_CHECKING:
    from sqlmesh.core.model.definition import Model
    from sqlmesh.utils import UniqueKeyDict
    from sqlmesh.utils.dag import DAG


def update_model_schemas(
    dag: DAG[str],
    models: UniqueKeyDict[str, Model],
    cache_dir: Path,
) -> None:
    schema = MappingSchema(normalize=False)
    optimized_query_cache: OptimizedQueryCache = OptimizedQueryCache(cache_dir)

    _update_model_schemas(dag, models, schema, optimized_query_cache)


def _update_schema_with_model(schema: MappingSchema, model: Model) -> None:
    columns_to_types = model.columns_to_types
    if columns_to_types:
        try:
            schema.add_table(model.fqn, columns_to_types, dialect=model.dialect)
        except SchemaError as e:
            if "nesting level:" in str(e):
                from sqlmesh.core.console import get_console

                get_console().log_error(
                    "SQLMesh requires all model names and references to have the same level of nesting."
                )
            raise


def _update_model_schemas(
    dag: DAG[str],
    models: UniqueKeyDict[str, Model],
    schema: MappingSchema,
    optimized_query_cache: OptimizedQueryCache,
) -> None:
    futures = set()
    graph = {
        model: {dep for dep in deps if dep in models}
        for model, deps in dag._dag.items()
        if model in models
    }

    def process_models(completed_model: t.Optional[Model] = None) -> None:
        for name in list(graph):
            deps = graph[name]

            if completed_model:
                deps.discard(completed_model.fqn)

            if not deps:
                del graph[name]
                model = models[name]
                futures.add(
                    executor.submit(
                        load_optimized_query_and_mapping,
                        model,
                        mapping={
                            parent: models[parent].columns_to_types
                            for parent in model.depends_on
                            if parent in models
                        },
                    )
                )

    with optimized_query_cache_pool(optimized_query_cache) as executor:
        process_models()

        while futures:
            for future in as_completed(futures):
                try:
                    futures.remove(future)
                    fqn, cache_entry, data_hash, metadata_hash, mapping_schema = future.result()
                    model = models[fqn]
                    model._data_hash = data_hash
                    model._metadata_hash = metadata_hash
                    if model.mapping_schema != mapping_schema:
                        model.set_mapping_schema(mapping_schema)
                    if cache_entry is not None:
                        # The worker has already decompressed and decoded this entry. Passing
                        # it back avoids a second gzip/pickle read in the parent process.
                        optimized_query_cache.with_optimized_query_entry(model, cache_entry)
                    _update_schema_with_model(schema, model)
                    process_models(completed_model=model)
                except Exception as ex:
                    raise SchemaError(f"Failed to update model schemas\n\n{ex}")
