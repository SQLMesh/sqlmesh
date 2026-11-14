from __future__ import annotations

import logging
import typing as t


from sqlglot import exp

from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.engine_adapter.postgres import PostgresEngineAdapter
from sqlmesh.core.engine_adapter.shared import (
    set_catalog,
    CatalogSupport,
    CommentCreationView,
    CommentCreationTable,
)

from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import TableName

logger = logging.getLogger(__name__)


@set_catalog()
class RisingwaveEngineAdapter(PostgresEngineAdapter):
    DIALECT = "risingwave"
    DEFAULT_BATCH_SIZE = 400
    CATALOG_SUPPORT = CatalogSupport.SINGLE_CATALOG_ONLY
    COMMENT_CREATION_TABLE = CommentCreationTable.COMMENT_COMMAND_ONLY
    COMMENT_CREATION_VIEW = CommentCreationView.UNSUPPORTED
    SUPPORTS_MATERIALIZED_VIEWS = True
    SUPPORTS_TRANSACTIONS = False
    MAX_IDENTIFIER_LENGTH = None
    SUPPORTS_GRANTS = False

    # COPY into a staging table hasn't been verified on RisingWave, so DataFrames keep using `VALUES`
    _df_to_source_queries = EngineAdapter._df_to_source_queries

    def columns(
        self, table_name: TableName, include_pseudo_columns: bool = False
    ) -> t.Dict[str, exp.DataType]:
        """Fetches column names and types for the target_table"""
        table = exp.to_table(table_name)

        sql = (
            exp.select("rw_columns.name AS column_name", "rw_columns.data_type AS data_type")
            .from_("rw_catalog.rw_columns")
            .join("rw_catalog.rw_relations", on="rw_relations.id=rw_columns.relation_id")
            .join("rw_catalog.rw_schemas", on="rw_schemas.id=rw_relations.schema_id")
            .where(
                exp.and_(
                    exp.column("name", table="rw_relations").eq(table.alias_or_name),
                    exp.column("name", table="rw_columns").neq("_row_id"),
                    exp.column("name", table="rw_columns").neq("_rw_timestamp"),
                )
            )
        )

        if table.db:
            sql = sql.where(exp.column("name", table="rw_schemas").eq(table.db))

        self.execute(sql)
        resp = self.cursor.fetchall()
        if not resp:
            raise SQLMeshError(f"Could not get columns for table {table_name}. Table not found.")
        return {
            column_name: exp.DataType.build(data_type, dialect=self.dialect, udt=True)
            for column_name, data_type in resp
        }

    def _truncate_table(self, table_name: TableName) -> None:
        return self.execute(exp.Delete(this=exp.to_table(table_name)))
