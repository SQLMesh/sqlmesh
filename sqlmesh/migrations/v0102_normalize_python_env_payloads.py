"""Re-normalize python_env payloads using ast.unparse after dropping astor.

SQLMesh previously used the third-party `astor` library to serialise Python
function source code (`normalize_source`). That library has been replaced with
the stdlib `ast.unparse`, which produces subtly different text for the same
AST (e.g. `lambda : x` → `lambda: x`, condensed multi-line signatures, etc.).

Because `python_env` payloads are included in each snapshot's `data_hash`,
any model that contains Python code (Python models, SQL models with Python
macros/signals) would otherwise appear as *Directly Modified* after the upgrade,
potentially triggering a full backfill.

This migration re-normalises every stored `Executable` payload of
`kind == "definition"` via `ast.unparse(ast.parse(payload))`. The
subsequent `_migrate_rows` pass then recomputes fingerprints from the updated
payloads so that they match what the current code produces when loading models
from disk. The migrated snapshots are flagged `migrated = True`, so no
unexpected backfills are scheduled.
"""

import ast
import json

from sqlglot import exp

from sqlmesh.utils.migration import index_text_type, blob_text_type


def migrate_schemas(engine_adapter, schema, **kwargs):  # type: ignore
    pass


def migrate_rows(engine_adapter, schema, **kwargs):  # type: ignore
    import pandas as pd

    snapshots_table = "_snapshots"
    if schema:
        snapshots_table = f"{schema}.{snapshots_table}"

    index_type = index_text_type(engine_adapter.dialect)
    blob_type = blob_text_type(engine_adapter.dialect)

    new_snapshots = []
    migration_needed = False

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
        forward_only,
        dev_version,
        fingerprint,
    ) in engine_adapter.fetchall(
        exp.select(
            "name",
            "identifier",
            "version",
            "snapshot",
            "kind_name",
            "updated_ts",
            "unpaused_ts",
            "ttl_ms",
            "unrestorable",
            "forward_only",
            "dev_version",
            "fingerprint",
        ).from_(snapshots_table),
        quote_identifiers=True,
    ):
        parsed_snapshot = json.loads(snapshot)
        python_env = parsed_snapshot["node"].get("python_env") or {}
        for executable in python_env.values():
            if executable.get("kind") != "definition":
                continue
            new_payload = ast.unparse(ast.parse(executable["payload"])).strip()
            if new_payload != executable["payload"]:
                executable["payload"] = new_payload
                migration_needed = True

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
                "forward_only": forward_only,
                "dev_version": dev_version,
                "fingerprint": fingerprint,
            }
        )

    if migration_needed and new_snapshots:
        engine_adapter.delete_from(snapshots_table, "TRUE")

        engine_adapter.insert_append(
            snapshots_table,
            pd.DataFrame(new_snapshots),
            target_columns_to_types={
                "name": exp.DataType.build(index_type),
                "identifier": exp.DataType.build(index_type),
                "version": exp.DataType.build(index_type),
                "snapshot": exp.DataType.build(blob_type),
                "kind_name": exp.DataType.build(index_type),
                "updated_ts": exp.DataType.build("bigint"),
                "unpaused_ts": exp.DataType.build("bigint"),
                "ttl_ms": exp.DataType.build("bigint"),
                "unrestorable": exp.DataType.build("boolean"),
                "forward_only": exp.DataType.build("boolean"),
                "dev_version": exp.DataType.build(index_type),
                "fingerprint": exp.DataType.build(blob_type),
            },
        )
