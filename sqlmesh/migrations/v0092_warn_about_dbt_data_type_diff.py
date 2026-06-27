"""
Warns dbt users about potential diffs due to corrected data_type handling.

SQLMesh previously treated dbt's schema.yml data_type field as columns_to_types, which
doesn't match dbt's behavior. dbt only uses data_type for contracts/validation, not DDL.
This fix may cause diffs if tables were created with incorrect types.

More context: https://github.com/SQLMesh/sqlmesh/pull/5231
"""

import json

from sqlglot import exp

from sqlmesh.core.console import get_console

SQLMESH_DBT_PACKAGE = "sqlmesh.dbt"


def migrate_schemas(engine_adapter, schema, **kwargs):  # type: ignore
    pass


def migrate_rows(engine_adapter, schema, **kwargs):  # type: ignore
    snapshots_table = "_snapshots"
    if schema:
        snapshots_table = f"{schema}.{snapshots_table}"

    warning = (
        "SQLMesh previously misinterpreted dbt's schema.yml 'data_type' field as actual "
        "column types, but dbt only uses these for contracts/validation, not in actual "
        "DDL statements. This has been fixed to match dbt's actual behavior. Your existing "
        "tables may have been created with incorrect column types. After this migration, run "
        "'sqlmesh diff prod' to check for column type differences, and if any are found, "
        "apply a plan to correct the table schemas. For more details, see: "
        "https://github.com/SQLMesh/sqlmesh/pull/5231."
    )

    for (snapshot,) in engine_adapter.fetchall(
        exp.select("snapshot").from_(snapshots_table), quote_identifiers=True
    ):
        parsed_snapshot = json.loads(snapshot)
        node = parsed_snapshot["node"]

        jinja_macros = node.get("jinja_macros") or {}
        create_builtins_module = jinja_macros.get("create_builtins_module") or ""

        if create_builtins_module == SQLMESH_DBT_PACKAGE and node.get("columns"):
            get_console().log_warning(warning)
            return
