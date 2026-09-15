from __future__ import annotations

import typing as t
import logging

from sqlglot import exp

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.base import EngineAdapter, _get_data_object_cache_key
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
)
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter._typing import QueryOrDF


logger = logging.getLogger(__name__)


class BasePostgresEngineAdapter(EngineAdapter):
    DEFAULT_BATCH_SIZE = 400
    COMMENT_CREATION_TABLE = CommentCreationTable.COMMENT_COMMAND_ONLY
    COMMENT_CREATION_VIEW = CommentCreationView.COMMENT_COMMAND_ONLY
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    SUPPORTED_DROP_CASCADE_OBJECT_KINDS = ["SCHEMA", "TABLE", "VIEW"]

    def columns(
        self, table_name: TableName, include_pseudo_columns: bool = False
    ) -> t.Dict[str, exp.DataType]:
        """Fetches column names and types for the target table."""
        table = exp.to_table(table_name)

        sql = (
            exp.select(
                "attname AS column_name",
                "pg_catalog.format_type(atttypid, atttypmod) AS data_type",
            )
            .from_("pg_catalog.pg_attribute")
            .join("pg_catalog.pg_class", on="pg_class.oid = attrelid")
            .join("pg_catalog.pg_namespace", on="pg_namespace.oid = relnamespace")
            .where(
                exp.and_(
                    "attnum > 0",
                    "NOT attisdropped",
                    exp.column("relname").eq(table.alias_or_name),
                )
            )
        )
        if table.args.get("db"):
            sql = sql.where(exp.column("nspname").eq(table.args["db"].name))

        self.execute(sql)
        resp = self.cursor.fetchall()
        if not resp:
            raise SQLMeshError(
                f"Could not get columns for table '{table.sql(dialect=self.dialect)}'. Table not found."
            )

        return {
            column_name: exp.DataType.build(data_type, dialect=self.dialect, udt=True)
            for column_name, data_type in resp
        }

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.SINGLE_CATALOG_ONLY

    def table_exists(self, table_name: TableName) -> bool:
        """
        Postgres doesn't support describe so I'm using what the redshift cursor does to check if a table
        exists. We don't use this directly in order for this to work as a base class for other postgres

        Reference: https://github.com/aws/amazon-redshift-python-driver/blob/master/redshift_connector/cursor.py#L528-L553
        """
        table = exp.to_table(table_name)
        data_object_cache_key = _get_data_object_cache_key(table.catalog, table.db, table.name)
        if data_object_cache_key in self._data_object_cache:
            logger.debug("Table existence cache hit: %s", data_object_cache_key)
            return self._data_object_cache[data_object_cache_key] is not None

        sql = (
            exp.select("1")
            .from_("information_schema.tables")
            .where(f"table_name = '{table.alias_or_name}'")
        )
        database_name = table.db
        if database_name:
            sql = sql.where(f"table_schema = '{database_name}'")

        self.execute(sql)

        result = self.cursor.fetchone()

        return result[0] == 1 if result is not None else False

    def create_view(
        self,
        view_name: TableName,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        replace: bool = True,
        materialized: bool = False,
        materialized_properties: t.Optional[t.Dict[str, t.Any]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        view_properties: t.Optional[t.Dict[str, exp.Expr]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **create_kwargs: t.Any,
    ) -> None:
        """
        Postgres has very strict rules around view replacement. For example the new query must generate an identical setFormatter
        of columns, using the same column names and data types as the old one. We have to delete the old view instead of replacing it
        to work around these constraints.

        Reference: https://www.postgresql.org/docs/current/sql-createview.html
        """
        with self.transaction():
            if replace:
                self.drop_view(view_name, materialized=materialized)
            super().create_view(
                view_name,
                query_or_df,
                target_columns_to_types=target_columns_to_types,
                replace=False,
                materialized=materialized,
                materialized_properties=materialized_properties,
                table_description=table_description,
                column_descriptions=column_descriptions,
                view_properties=view_properties,
                source_columns=source_columns,
                **create_kwargs,
            )

    def drop_view(
        self,
        view_name: TableName,
        ignore_if_not_exists: bool = True,
        materialized: bool = False,
        **kwargs: t.Any,
    ) -> None:
        kwargs["cascade"] = kwargs.get("cascade", True)
        return super().drop_view(
            view_name,
            ignore_if_not_exists=ignore_if_not_exists,
            materialized=materialized,
            **kwargs,
        )

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and optionally catalog.
        """
        catalog = self.get_current_catalog()
        table_query = exp.select(
            exp.column("schemaname").as_("schema_name"),
            exp.column("tablename").as_("name"),
            exp.Literal.string("TABLE").as_("type"),
        ).from_("pg_tables")
        view_query = exp.select(
            exp.column("schemaname").as_("schema_name"),
            exp.column("viewname").as_("name"),
            exp.Literal.string("VIEW").as_("type"),
        ).from_("pg_views")
        materialized_view_query = exp.select(
            exp.column("schemaname").as_("schema_name"),
            exp.column("matviewname").as_("name"),
            exp.Literal.string("MATERIALIZED_VIEW").as_("type"),
        ).from_("pg_matviews")
        subquery = exp.union(
            table_query,
            exp.union(view_query, materialized_view_query, distinct=False),
            distinct=False,
        )
        query = (
            exp.select("*")
            .from_(subquery.subquery(alias="objs"))
            .where(exp.column("schema_name").eq(to_schema(schema_name).db))
        )
        if object_names:
            query = query.where(exp.column("name").isin(*object_names))
        df = self.fetchdf(query)
        return [
            DataObject(
                catalog=catalog,
                schema=row.schema_name,
                name=row.name,
                type=DataObjectType.from_str(row.type),  # type: ignore
            )
            for row in df.itertuples()
        ]

    def _get_current_schema(self) -> str:
        """Returns the current default schema for the connection."""
        result = self.fetchone(exp.select(exp.func("current_schema")))
        if result and result[0]:
            return result[0]
        return "public"
