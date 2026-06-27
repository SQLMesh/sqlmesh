from __future__ import annotations

import logging
import typing as t

from sqlglot import exp, parse_one

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.mixins import (
    LogicalMergeMixin,
    NonTransactionalTruncateMixin,
    PandasNativeFetchDFSupportMixin,
    RowDiffMixin,
)
from sqlmesh.core.engine_adapter.shared import (
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
    set_catalog,
)

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName

logger = logging.getLogger(__name__)


@set_catalog()
class MySQLEngineAdapter(
    LogicalMergeMixin, PandasNativeFetchDFSupportMixin, NonTransactionalTruncateMixin, RowDiffMixin
):
    DEFAULT_BATCH_SIZE = 200
    DIALECT = "mysql"
    SUPPORTS_INDEXES = True
    COMMENT_CREATION_TABLE = CommentCreationTable.IN_SCHEMA_DEF_NO_CTAS
    COMMENT_CREATION_VIEW = CommentCreationView.UNSUPPORTED
    MAX_TABLE_COMMENT_LENGTH = 2048
    MAX_COLUMN_COMMENT_LENGTH = 1024
    SUPPORTS_REPLACE_TABLE = False
    MAX_IDENTIFIER_LENGTH = 64
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            exp.DataType.build("BIT", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("BINARY", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(10, 0), (0,)],
            exp.DataType.build("CHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("NCHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("TEXT", dialect=DIALECT).this: [(65535,)],
            exp.DataType.build("TIME", dialect=DIALECT).this: [(0,)],
            exp.DataType.build("DATETIME", dialect=DIALECT).this: [(0,)],
            exp.DataType.build("TIMESTAMP", dialect=DIALECT).this: [(0,)],
        },
    }

    def get_current_catalog(self) -> t.Optional[str]:
        """Returns the catalog name of the current connection."""
        return None

    def create_index(
        self,
        table_name: TableName,
        index_name: str,
        columns: t.Tuple[str, ...],
        exists: bool = True,
    ) -> None:
        # MySQL doesn't support IF EXISTS clause for indexes.
        super().create_index(table_name, index_name, columns, exists=False)

    def drop_schema(
        self,
        schema_name: SchemaName,
        ignore_if_not_exists: bool = True,
        cascade: bool = False,
        **drop_args: t.Dict[str, exp.Expr],
    ) -> None:
        # MySQL doesn't support CASCADE clause and drops schemas unconditionally.
        super().drop_schema(schema_name, ignore_if_not_exists=ignore_if_not_exists, cascade=False)

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and optionally catalog.
        """
        query = (
            exp.select(
                exp.column("table_name").as_("name"),
                exp.column("table_schema").as_("schema_name"),
                exp.case()
                .when(
                    exp.column("table_type").eq("BASE TABLE"),
                    exp.Literal.string("table"),
                )
                .when(
                    exp.column("table_type").eq("VIEW"),
                    exp.Literal.string("view"),
                )
                .else_("table_type")
                .as_("type"),
            )
            .from_(exp.table_("tables", db="information_schema"))
            .where(exp.column("table_schema").eq(to_schema(schema_name).db))
        )
        if object_names:
            query = query.where(exp.column("table_name").isin(*object_names))
        df = self.fetchdf(query)
        return [
            DataObject(
                schema=row.schema_name,
                name=row.name,
                type=DataObjectType.from_str(row.type),  # type: ignore
            )
            for row in df.itertuples()
        ]

    def _build_create_comment_table_exp(
        self, table: exp.Table, table_comment: str, table_kind: str
    ) -> exp.Comment | str:
        table_sql = table.sql(dialect=self.dialect, identify=True)

        truncated_comment = self._truncate_table_comment(table_comment)
        comment_sql = exp.Literal.string(truncated_comment).sql(dialect=self.dialect)

        return f"ALTER TABLE {table_sql} COMMENT = {comment_sql}"

    def _create_column_comments(
        self,
        table_name: TableName,
        column_comments: t.Dict[str, str],
        table_kind: str = "TABLE",
        materialized_view: bool = False,
    ) -> None:
        table = exp.to_table(table_name)
        table_sql = table.sql(dialect=self.dialect, identify=True)

        # MySQL ALTER TABLE MODIFY completely replaces the column (overwriting options and constraints).
        # self.columns() only returns the column types so doesn't allow us to fully/correctly replace a column definition.
        # To get the full column definition we retrieve and parse the table's CREATE TABLE statement.
        create_table_exp = parse_one(
            self.fetchone(f"SHOW CREATE TABLE {table_sql}")[1],  # type: ignore
            dialect=self.dialect,
        )
        col_def_exps = {
            col_def.name: col_def.copy()
            for col_def in create_table_exp.find(exp.Schema).find_all(exp.ColumnDef)  # type: ignore
        }

        for col in column_comments:
            col_def = col_def_exps.get(col)
            if col_def:
                col_def.args["constraints"].extend(
                    self._build_col_comment_exp(col_def.alias_or_name, column_comments)
                )

                try:
                    self.execute(
                        f"ALTER TABLE {table_sql} MODIFY {col_def.sql(dialect=self.dialect, identify=True)}",
                    )
                except Exception:
                    logger.warning(
                        f"Column comments for column '{col_def.alias_or_name}' in table '{table.alias_or_name}' not registered - this may be due to limited permissions.",
                        exc_info=True,
                    )

    def _create_table_like(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        exists: bool,
        **kwargs: t.Any,
    ) -> None:
        self.execute(
            exp.Create(
                this=exp.to_table(target_table_name),
                kind="TABLE",
                exists=exists,
                properties=exp.Properties(
                    expressions=[
                        exp.LikeProperty(
                            this=exp.to_table(source_table_name),
                        ),
                    ],
                ),
            )
        )

    def ping(self) -> None:
        self._connection_pool.get().ping(reconnect=False)
