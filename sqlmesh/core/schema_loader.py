from __future__ import annotations

import typing as t
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlglot import exp
from sqlglot.dialects.dialect import DialectType

from sqlmesh.core.console import get_console
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.model.definition import Model
from sqlmesh.core.state_sync import StateReader
from sqlmesh.utils import UniqueKeyDict, yaml
from sqlmesh.utils.errors import SQLMeshError


def create_external_models_file(
    path: Path,
    models: UniqueKeyDict[str, Model],
    adapter: EngineAdapter,
    state_reader: StateReader,
    dialect: DialectType,
    gateway: t.Optional[str] = None,
    max_workers: int = 1,
    strict: bool = False,
    all_models: t.Optional[t.Dict[str, Model]] = None,
) -> None:
    """Create or replace a YAML file with column and types of all columns in all external models.

    Args:
        path: The path to store the YAML file.
        models: FQN to model for the current repo/config being processed.
        adapter: The engine adapter.
        state_reader: The state reader.
        dialect: The dialect to serialize the schema as.
        gateway: If the model should be associated with a specific gateway; the gateway key
        max_workers: The max concurrent workers to fetch columns.
        strict: If True, raise an error if the external model is missing in the database.
        all_models: FQN to model across all loaded repos. When provided, a dependency is only
            classified as external if it is absent from this full set. This prevents cross-repo
            internal models from being misclassified as external in multi-repo setups.
    """
    known_models: t.Dict[str, Model] = all_models if all_models is not None else models
    external_model_fqns = set()

    for fqn, model in models.items():
        if model.kind.is_external:
            external_model_fqns.add(fqn)
        for dep in model.depends_on:
            if dep not in known_models:
                external_model_fqns.add(dep)

    # Make sure we don't convert internal models into external ones.
    existing_model_fqns = state_reader.nodes_exist(external_model_fqns, exclude_external=True)
    if existing_model_fqns:
        existing_model_fqns_str = ", ".join(existing_model_fqns)
        get_console().log_warning(
            f"The following models already exist and can't be converted to external: {existing_model_fqns_str}. "
            "Perhaps these models have been removed, while downstream models that reference them weren't updated accordingly."
        )
        external_model_fqns -= existing_model_fqns

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        gateway_part = {"gateway": gateway} if gateway else {}

        schemas = [
            {
                "name": exp.to_table(table).sql(dialect=dialect),
                "columns": columns,
                **gateway_part,
            }
            for table, columns in sorted(
                pool.map(
                    lambda table: (table, get_columns(adapter, dialect, table, strict)),
                    external_model_fqns,
                )
            )
            if columns
        ]

        # dont clobber existing entries from other gateways
        entries_to_keep = (
            [e for e in yaml.load(path) if e.get("gateway", None) != gateway]
            if path.exists()
            else []
        )

        with open(path, "w", encoding="utf-8") as file:
            yaml.dump(entries_to_keep + schemas, file)


def get_columns(
    adapter: EngineAdapter, dialect: DialectType, table: str, strict: bool
) -> t.Optional[t.Dict[str, t.Any]]:
    """
    Return the column and their types in a dictionary
    """
    try:
        columns = adapter.columns(table, include_pseudo_columns=True)
        return {c: dtype.sql(dialect=dialect) for c, dtype in columns.items()}
    except Exception as e:
        msg = f"Unable to get schema for '{table}': '{e}'."
        if strict:
            raise SQLMeshError(msg) from e
        get_console().log_warning(msg)
        return None
