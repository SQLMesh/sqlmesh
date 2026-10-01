"""Add dev version to the intervals table."""

import typing as t
import json
import zlib

from sqlglot import exp
from sqlmesh.utils.migration import index_text_type, blob_text_type


def migrate_schemas(engine_adapter, schema, **kwargs):  # type: ignore
    intervals_table = "_intervals"
    if schema:
        intervals_table = f"{schema}.{intervals_table}"

    index_type = index_text_type(engine_adapter.dialect)
    alter_table_exp = exp.Alter(
        this=exp.to_table(intervals_table),
        kind="TABLE",
        actions=[
            exp.ColumnDef(
                this=exp.to_column("dev_version"),
                kind=exp.DataType.build(index_type),
            )
        ],
    )
    engine_adapter.execute(alter_table_exp)


def migrate_rows(engine_adapter, schema, **kwargs):  # type: ignore
    intervals_table = "_intervals"
    snapshots_table = "_snapshots"
    if schema:
        intervals_table = f"{schema}.{intervals_table}"
        snapshots_table = f"{schema}.{snapshots_table}"

    used_dev_versions: t.Set[t.Tuple[str, str]] = set()
    used_versions: t.Set[t.Tuple[str, str]] = set()
    used_snapshot_ids: t.Set[t.Tuple[str, str]] = set()
    snapshot_ids_to_dev_versions: t.Dict[t.Tuple[str, str], str] = {}

    _migrate_snapshots(
        engine_adapter,
        snapshots_table,
        used_dev_versions,
        used_versions,
        used_snapshot_ids,
        snapshot_ids_to_dev_versions,
    )
    _migrate_intervals(
        engine_adapter,
        intervals_table,
        used_dev_versions,
        used_versions,
        used_snapshot_ids,
        snapshot_ids_to_dev_versions,
    )


def _migrate_intervals(
    engine_adapter: t.Any,
    intervals_table: str,
    used_dev_versions: t.Set[t.Tuple[str, str]],
    used_versions: t.Set[t.Tuple[str, str]],
    used_snapshot_ids: t.Set[t.Tuple[str, str]],
    snapshot_ids_to_dev_versions: t.Dict[t.Tuple[str, str], str],
) -> None:
    import pandas as pd

    index_type = index_text_type(engine_adapter.dialect)
    intervals_columns_to_types = {
        "id": exp.DataType.build(index_type),
        "created_ts": exp.DataType.build("bigint"),
        "name": exp.DataType.build(index_type),
        "identifier": exp.DataType.build("text"),
        "version": exp.DataType.build(index_type),
        "dev_version": exp.DataType.build(index_type),
        "start_ts": exp.DataType.build("bigint"),
        "end_ts": exp.DataType.build("bigint"),
        "is_dev": exp.DataType.build("boolean"),
        "is_removed": exp.DataType.build("boolean"),
        "is_compacted": exp.DataType.build("boolean"),
        "is_pending_restatement": exp.DataType.build("boolean"),
    }

    new_intervals = []
    for (
        interval_id,
        created_ts,
        name,
        identifier,
        version,
        _,
        start_ts,
        end_ts,
        is_dev,
        is_removed,
        is_compacted,
        is_pending_restatement,
    ) in engine_adapter.fetchall(
        exp.select(*intervals_columns_to_types).from_(intervals_table),
        quote_identifiers=True,
    ):
        if (name, version) not in used_versions:
            # If the interval's version is no longer used, we can safely delete it
            continue

        dev_version = snapshot_ids_to_dev_versions.get((name, identifier))
        if (name, dev_version) not in used_dev_versions and is_dev:
            # If the interval's dev version is no longer used and this is a dev interval, we can safely delete it
            continue

        if (name, identifier) not in used_snapshot_ids:
            # If the snapshot associated with this interval no longer exists, we can nullify the interval's identifier
            # to improve compaction
            is_compacted = False
            identifier = None
            if not is_dev:
                # If the interval is not dev, we can safely nullify the dev version as well
                dev_version = None

        new_intervals.append(
            {
                "id": interval_id,
                "created_ts": created_ts,
                "name": name,
                "identifier": identifier,
                "version": version,
                "dev_version": dev_version,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "is_dev": is_dev,
                "is_removed": is_removed,
                "is_compacted": is_compacted,
                "is_pending_restatement": is_pending_restatement,
            }
        )

    if new_intervals:
        engine_adapter.delete_from(intervals_table, "TRUE")
        engine_adapter.insert_append(
            intervals_table,
            pd.DataFrame(new_intervals),
            target_columns_to_types=intervals_columns_to_types,
        )


def _migrate_snapshots(
    engine_adapter: t.Any,
    snapshots_table: str,
    used_dev_versions: t.Set[t.Tuple[str, str]],
    used_versions: t.Set[t.Tuple[str, str]],
    used_snapshot_ids: t.Set[t.Tuple[str, str]],
    snapshot_ids_to_dev_versions: t.Dict[t.Tuple[str, str], str],
) -> None:
    import pandas as pd

    index_type = index_text_type(engine_adapter.dialect)
    blob_type = blob_text_type(engine_adapter.dialect)
    snapshots_columns_to_types = {
        "name": exp.DataType.build(index_type),
        "identifier": exp.DataType.build(index_type),
        "version": exp.DataType.build(index_type),
        "snapshot": exp.DataType.build(blob_type),
        "kind_name": exp.DataType.build(index_type),
        "updated_ts": exp.DataType.build("bigint"),
        "unpaused_ts": exp.DataType.build("bigint"),
        "ttl_ms": exp.DataType.build("bigint"),
        "unrestorable": exp.DataType.build("boolean"),
    }

    new_snapshots = []
    for (
        name,
        identifier,
        version,
        snapshot,
        kind_name,
        updated_ts,
        unpaused_ts,
        ttl_ms,
        unrestorable,
    ) in engine_adapter.fetchall(
        exp.select(*snapshots_columns_to_types).from_(snapshots_table),
        quote_identifiers=True,
    ):
        parsed_snapshot = json.loads(snapshot)
        version = parsed_snapshot.get("version") or version
        dev_version = get_dev_version(parsed_snapshot)
        parsed_snapshot["dev_version"] = dev_version
        parsed_snapshot["version"] = version

        used_dev_versions.add((name, dev_version))
        used_versions.add((name, version))
        used_snapshot_ids.add((name, identifier))
        snapshot_ids_to_dev_versions[(name, identifier)] = dev_version

        for previous_version in parsed_snapshot.get("previous_versions", []):
            previous_identifier = get_identifier(previous_version)
            previous_dev_version = get_dev_version(previous_version)
            snapshot_ids_to_dev_versions[(name, previous_identifier)] = previous_dev_version

        new_snapshots.append(
            {
                "name": name,
                "identifier": identifier,
                "version": version,
                "snapshot": json.dumps(parsed_snapshot),
                "kind_name": kind_name,
                "updated_ts": updated_ts,
                "unpaused_ts": unpaused_ts,
                "ttl_ms": ttl_ms,
                "unrestorable": unrestorable,
            }
        )

    if new_snapshots:
        engine_adapter.delete_from(snapshots_table, "TRUE")
        engine_adapter.insert_append(
            snapshots_table,
            pd.DataFrame(new_snapshots),
            target_columns_to_types=snapshots_columns_to_types,
        )


def get_identifier(snapshot: t.Dict[str, t.Any]) -> str:
    fingerprint = snapshot["fingerprint"]
    return crc32(
        [
            fingerprint["data_hash"],
            fingerprint["metadata_hash"],
            fingerprint["parent_data_hash"],
            fingerprint["parent_metadata_hash"],
        ]
    )


def get_dev_version(snapshot: t.Dict[str, t.Any]) -> str:
    dev_version = snapshot.get("dev_version")
    if dev_version:
        return dev_version
    fingerprint = snapshot["fingerprint"]
    return crc32([fingerprint["data_hash"], fingerprint["parent_data_hash"]])


def crc32(data: t.Iterable[t.Optional[str]]) -> str:
    return str(zlib.crc32(safe_concat(data)))


def safe_concat(data: t.Iterable[t.Optional[str]]) -> bytes:
    return ";".join("" if d is None else d for d in data).encode("utf-8")
