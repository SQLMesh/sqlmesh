from __future__ import annotations

import typing as t
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path

from sqlglot import exp
from sqlglot.dialects.dialect import DialectType

from sqlmesh.core.console import get_console
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.core.model.definition import Model
from sqlmesh.core.state_sync import StateReader
from sqlmesh.utils import UniqueKeyDict, classproperty, yaml
from sqlmesh.utils.errors import SQLMeshError


class CreateExternalModelsMode(str, Enum):
    """Determines how existing entries are handled when creating external models."""

    OVERWRITE = "overwrite"
    SYNC = "sync"
    SYNC_PRUNE = "sync_prune"

    @classproperty
    def default(cls) -> CreateExternalModelsMode:
        return CreateExternalModelsMode.OVERWRITE

    @property
    def is_overwrite(self) -> bool:
        return self == CreateExternalModelsMode.OVERWRITE

    @property
    def is_sync(self) -> bool:
        return self == CreateExternalModelsMode.SYNC

    @property
    def is_sync_prune(self) -> bool:
        return self == CreateExternalModelsMode.SYNC_PRUNE


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
    mode: CreateExternalModelsMode = CreateExternalModelsMode.default,
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
        mode: The mode for updating external models. "overwrite" replaces all entries (default),
            "sync" syncs columns while preserving metadata and warns on stale entries,
            "sync_prune" also removes stale entries.
    """
    known_models: t.Dict[str, Model] = all_models if all_models is not None else models

    external_model_fqns = {
        dep
        for model in models.values()
        for dep in model.depends_on
        if dep not in known_models or known_models[dep].kind.is_external
    }

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

        if mode.is_overwrite:
            # dont clobber existing entries from other gateways
            entries_to_keep = (
                [e for e in yaml.load(path) if e.get("gateway", None) != gateway]
                if path.exists()
                else []
            )

            with open(path, "w", encoding="utf-8") as file:
                yaml.dump(entries_to_keep + schemas, file)
        else:
            _write_external_models_file_update(path, schemas, gateway, mode=mode)


def _write_external_models_file_update(
    path: Path,
    schemas: t.List[t.Dict[str, t.Any]],
    gateway: t.Optional[str],
    mode: CreateExternalModelsMode,
) -> None:
    """Write external models file for sync and sync_prune modes.

    Preserves hand-edited metadata on existing entries, only syncing columns from the DB.
    sync mode warns about stale entries but keeps them.
    sync_prune mode removes stale entries.
    """
    existing_entries = yaml.load(path) if path.exists() else []

    other_gateway_entries: t.List[t.Dict[str, t.Any]] = []
    same_gateway_entries: t.List[t.Dict[str, t.Any]] = []

    for entry in existing_entries:
        if entry.get("gateway", None) != gateway:
            other_gateway_entries.append(entry)
        else:
            same_gateway_entries.append(entry)

    same_gateway_by_name: t.Dict[str, t.Dict[str, t.Any]] = {}
    for entry in same_gateway_entries:
        same_gateway_by_name[entry["name"]] = entry

    result = list(other_gateway_entries)
    matched_in_schemas: t.Set[str] = {s["name"] for s in schemas}

    for new_schema in schemas:
        name = new_schema["name"]
        if name in same_gateway_by_name:
            existing = same_gateway_by_name[name]
            existing["columns"] = new_schema["columns"]
            result.append(existing)
        else:
            result.append(new_schema)

    stale_count = 0
    for entry in same_gateway_entries:
        name = entry["name"]
        if name not in matched_in_schemas:
            stale_count += 1
            if mode.is_sync:
                get_console().log_warning(
                    f"External model '{name}' is no longer referenced but was preserved. "
                    "Use --mode sync_prune to automatically remove stale entries."
                )
                result.append(entry)

    if stale_count and mode.is_sync_prune:
        get_console().log_warning(
            f"Removed {stale_count} stale external model(s) that are no longer referenced."
        )

    with open(path, "w", encoding="utf-8") as file:
        yaml.dump(result, file)


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
