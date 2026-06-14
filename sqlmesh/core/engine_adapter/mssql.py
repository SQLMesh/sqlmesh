"""Contains MSSQLEngineAdapter."""

from __future__ import annotations

import typing as t
import logging

from sqlglot import exp

from sqlmesh.core.dialect import to_schema, add_table
from sqlmesh.core.engine_adapter.base import (
    EngineAdapterWithIndexSupport,
    EngineAdapter,
    InsertOverwriteStrategy,
    MERGE_SOURCE_ALIAS,
    MERGE_TARGET_ALIAS,
    _get_data_object_cache_key,
)
from sqlmesh.core.engine_adapter.mixins import (
    GetCurrentCatalogFromFunctionMixin,
    PandasNativeFetchDFSupportMixin,
    VarcharSizeWorkaroundMixin,
    RowDiffMixin,
)
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
    SourceQuery,
    set_catalog,
)
from sqlmesh.utils import get_source_columns_to_types

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter._typing import DF, Query, QueryOrDF


logger = logging.getLogger(__name__)


@set_catalog()
class MSSQLEngineAdapter(
    EngineAdapterWithIndexSupport,
    PandasNativeFetchDFSupportMixin,
    GetCurrentCatalogFromFunctionMixin,
    VarcharSizeWorkaroundMixin,
    RowDiffMixin,
):
    DIALECT: str = "tsql"
    SUPPORTS_TUPLE_IN = False
    SUPPORTS_MATERIALIZED_VIEWS = False
    CURRENT_CATALOG_EXPRESSION = exp.func("db_name")
    COMMENT_CREATION_TABLE = CommentCreationTable.UNSUPPORTED
    COMMENT_CREATION_VIEW = CommentCreationView.UNSUPPORTED
    SUPPORTS_REPLACE_TABLE = False
    MAX_IDENTIFIER_LENGTH = 128
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(18, 0), (0,)],
            exp.DataType.build("BINARY", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("VARBINARY", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("CHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("NCHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("NVARCHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("TIME", dialect=DIALECT).this: [(7,)],
            exp.DataType.build("DATETIME2", dialect=DIALECT).this: [(7,)],
            exp.DataType.build("DATETIMEOFFSET", dialect=DIALECT).this: [(7,)],
        },
        "max_parameter_length": {
            exp.DataType.build("VARBINARY", dialect=DIALECT).this: 2147483647,  # 2 GB
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: 2147483647,
            exp.DataType.build("NVARCHAR", dialect=DIALECT).this: 2147483647,
        },
    }
    VARIABLE_LENGTH_DATA_TYPES = {"binary", "varbinary", "char", "varchar", "nchar", "nvarchar"}
    INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.MERGE

    @property
    def catalog_support(self) -> CatalogSupport:
        # MSSQL and AzureSQL both use this engine adapter, but they differ in catalog support.
        # Therefore, we specify the catalog support in the connection config `_extra_engine_config`
        # instead of in the adapter itself.
        return self._extra_config["catalog_support"]

    def columns(
        self,
        table_name: TableName,
        include_pseudo_columns: bool = True,
    ) -> t.Dict[str, exp.DataType]:
        """MsSql doesn't support describe so we query information_schema."""

        table = exp.to_table(table_name)

        sql = (
            exp.select(
                "COLUMN_NAME",
                "DATA_TYPE",
                "CHARACTER_MAXIMUM_LENGTH",
                "NUMERIC_PRECISION",
                "NUMERIC_SCALE",
            )
            .from_("INFORMATION_SCHEMA.COLUMNS")
            .where(f"TABLE_NAME = '{table.name}'")
        )
        database_name = table.db
        if database_name:
            sql = sql.where(f"TABLE_SCHEMA = '{database_name}'")

        columns_raw = self.fetchall(sql, quote_identifiers=True)

        def build_var_length_col(
            column_name: str,
            data_type: str,
            character_maximum_length: t.Optional[int] = None,
            numeric_precision: t.Optional[int] = None,
            numeric_scale: t.Optional[int] = None,
        ) -> tuple:
            data_type = data_type.lower()
            if (
                data_type in self.VARIABLE_LENGTH_DATA_TYPES
                and character_maximum_length is not None
                and character_maximum_length > 0
            ):
                return (column_name, f"{data_type}({character_maximum_length})")
            if (
                data_type in ("varbinary", "varchar", "nvarchar")
                and character_maximum_length is not None
                and character_maximum_length == -1
            ):
                return (column_name, f"{data_type}(max)")
            if data_type in ("decimal", "numeric"):
                return (column_name, f"{data_type}({numeric_precision}, {numeric_scale})")
            if data_type == "float":
                return (column_name, f"{data_type}({numeric_precision})")

            return (column_name, data_type)

        columns = [build_var_length_col(*row) for row in columns_raw]

        return {
            column_name: exp.DataType.build(data_type, dialect=self.dialect)
            for column_name, data_type in columns
        }

    def table_exists(self, table_name: TableName) -> bool:
        """MsSql doesn't support describe so we query information_schema."""
        table = exp.to_table(table_name)
        data_object_cache_key = _get_data_object_cache_key(table.catalog, table.db, table.name)
        if data_object_cache_key in self._data_object_cache:
            logger.debug("Table existence cache hit: %s", data_object_cache_key)
            return self._data_object_cache[data_object_cache_key] is not None

        sql = (
            exp.select("1")
            .from_("INFORMATION_SCHEMA.TABLES")
            .where(f"TABLE_NAME = '{table.alias_or_name}'")
        )
        database_name = table.db
        if database_name:
            sql = sql.where(f"TABLE_SCHEMA = '{database_name}'")

        result = self.fetchone(sql, quote_identifiers=True)

        return result[0] == 1 if result else False

    def set_current_catalog(self, catalog_name: str) -> None:
        self.execute(exp.Use(this=exp.to_identifier(catalog_name)))

    def drop_schema(
        self,
        schema_name: SchemaName,
        ignore_if_not_exists: bool = True,
        cascade: bool = False,
        **drop_args: t.Dict[str, exp.Expr],
    ) -> None:
        """
        MsSql doesn't support CASCADE clause and drops schemas unconditionally.
        """
        if cascade:
            objects = self._get_data_objects(schema_name)
            for obj in objects:
                # Build properly quoted table for MSSQL using square brackets when needed
                object_table = exp.table_(obj.name, obj.schema_name)

                # _get_data_objects is catalog-specific, so these can't accidentally drop view/tables in another catalog
                if obj.type == DataObjectType.VIEW:
                    self.drop_view(
                        object_table,
                        ignore_if_not_exists=ignore_if_not_exists,
                    )
                else:
                    self.drop_table(
                        object_table,
                        exists=ignore_if_not_exists,
                    )
        super().drop_schema(schema_name, ignore_if_not_exists=ignore_if_not_exists, cascade=False)

    def merge(
        self,
        target_table: TableName,
        source_table: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]],
        unique_key: t.Sequence[exp.Expr],
        when_matched: t.Optional[exp.Whens] = None,
        merge_filter: t.Optional[exp.Expr] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        mssql_merge_exists = kwargs.get("physical_properties", {}).get("mssql_merge_exists")

        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            source_table,
            target_columns_to_types,
            target_table=target_table,
            source_columns=source_columns,
        )
        target_columns_to_types = target_columns_to_types or self.columns(target_table)
        on = exp.and_(
            *(
                add_table(part, MERGE_TARGET_ALIAS).eq(add_table(part, MERGE_SOURCE_ALIAS))
                for part in unique_key
            )
        )
        if merge_filter:
            on = exp.and_(merge_filter, on)

        match_expressions = []
        if not when_matched:
            unique_key_names = [y.name for y in unique_key]
            columns_to_types_no_keys = [
                c for c in target_columns_to_types if c not in unique_key_names
            ]

            target_columns_no_keys = [
                exp.column(c, MERGE_TARGET_ALIAS) for c in columns_to_types_no_keys
            ]
            source_columns_no_keys = [
                exp.column(c, MERGE_SOURCE_ALIAS) for c in columns_to_types_no_keys
            ]

            match_condition = (
                exp.Exists(
                    this=exp.select(*target_columns_no_keys).except_(
                        exp.select(*source_columns_no_keys)
                    )
                )
                if mssql_merge_exists
                else None
            )

            if target_columns_no_keys:
                match_expressions.append(
                    exp.When(
                        matched=True,
                        source=False,
                        condition=match_condition,
                        then=exp.Update(
                            expressions=[
                                exp.column(col, MERGE_TARGET_ALIAS).eq(
                                    exp.column(col, MERGE_SOURCE_ALIAS)
                                )
                                for col in columns_to_types_no_keys
                            ],
                        ),
                    )
                )
        else:
            match_expressions.extend(when_matched.copy().expressions)

        match_expressions.append(
            exp.When(
                matched=False,
                source=False,
                then=exp.Insert(
                    this=exp.Tuple(
                        expressions=[exp.column(col) for col in target_columns_to_types]
                    ),
                    expression=exp.Tuple(
                        expressions=[
                            exp.column(col, MERGE_SOURCE_ALIAS) for col in target_columns_to_types
                        ]
                    ),
                ),
            )
        )
        for source_query in source_queries:
            with source_query as query:
                self._merge(
                    target_table=target_table,
                    query=query,
                    on=on,
                    whens=exp.Whens(expressions=match_expressions),
                )

    def _convert_df_datetime(self, df: DF, columns_to_types: t.Dict[str, exp.DataType]) -> None:
        import pandas as pd
        from pandas.api.types import is_datetime64_any_dtype  # type: ignore

        # pymssql doesn't convert Pandas Timestamp (datetime64) types
        # - this code is based on snowflake adapter implementation
        for column, kind in columns_to_types.items():
            # pymssql errors if the column contains a datetime.date object
            if kind.is_type("date"):  # type: ignore
                df[column] = pd.to_datetime(df[column]).dt.strftime("%Y-%m-%d")  # type: ignore
            elif is_datetime64_any_dtype(df.dtypes[column]):  # type: ignore
                if getattr(df.dtypes[column], "tz", None) is not None:  # type: ignore
                    # MSSQL requires a colon in the offset (+00:00) so we use isoformat() instead of strftime()
                    df[column] = pd.to_datetime(df[column]).map(lambda x: x.isoformat(" "))  # type: ignore

                    # bulk_copy() doesn't work with TZ timestamp, so load into string column and cast to
                    # timestamp in SELECT statement
                    columns_to_types[column] = exp.DataType.build("TEXT")
                else:
                    df[column] = pd.to_datetime(df[column]).dt.strftime("%Y-%m-%d %H:%M:%S.%f")  # type: ignore

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        import pandas as pd
        import numpy as np

        assert isinstance(df, pd.DataFrame)
        temp_table = self._get_temp_table(target_table or "pandas")

        # Return the superclass implementation if the connection pool doesn't support bulk_copy
        if not hasattr(self._connection_pool.get(), "bulk_copy"):
            return super()._df_to_source_queries(
                df, target_columns_to_types, batch_size, target_table, source_columns=source_columns
            )

        def query_factory() -> Query:
            # It is possible for the factory to be called multiple times and if so then the temp table will already
            # be created so we skip creating again. This means we are assuming the first call is the same result
            # as later calls.
            if not self.table_exists(temp_table):
                source_columns_to_types = get_source_columns_to_types(
                    target_columns_to_types, source_columns
                )
                ordered_df = df[
                    list(source_columns_to_types)
                ]  # reorder DataFrame so it matches columns_to_types
                self._convert_df_datetime(ordered_df, source_columns_to_types)
                self.create_table(temp_table, source_columns_to_types)
                rows: t.List[t.Tuple[t.Any, ...]] = list(
                    ordered_df.replace({np.nan: None}).itertuples(index=False, name=None)  # type: ignore
                )
                conn = self._connection_pool.get()
                conn.bulk_copy(temp_table.sql(dialect=self.dialect), rows)
            return exp.select(
                *self._casted_columns(target_columns_to_types, source_columns=source_columns)
            ).from_(temp_table)  # type: ignore

        return [
            SourceQuery(
                query_factory=query_factory,
                cleanup_func=lambda: self.drop_table(temp_table),
            )
        ]

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and catalog.
        """
        import pandas as pd

        catalog = self.get_current_catalog()
        query = (
            exp.select(
                exp.column("TABLE_NAME").as_("name"),
                exp.column("TABLE_SCHEMA").as_("schema_name"),
                exp.case()
                .when(exp.column("TABLE_TYPE").eq("BASE TABLE"), exp.Literal.string("TABLE"))
                .else_(exp.column("TABLE_TYPE"))
                .as_("type"),
            )
            .from_(exp.table_("TABLES", db="INFORMATION_SCHEMA"))
            .where(exp.column("TABLE_SCHEMA").eq(to_schema(schema_name).db))
        )
        if object_names:
            query = query.where(exp.column("TABLE_NAME").isin(*object_names))
        dataframe: pd.DataFrame = self.fetchdf(query)
        return [
            DataObject(
                catalog=catalog,  # type: ignore
                schema=row.schema_name,  # type: ignore
                name=row.name,  # type: ignore
                type=DataObjectType.from_str(row.type),  # type: ignore
            )
            for row in dataframe.itertuples()
        ]

    def _to_sql(self, expression: exp.Expr, quote: bool = True, **kwargs: t.Any) -> str:
        sql = super()._to_sql(expression, quote=quote, **kwargs)
        return f"{sql};"

    def _rename_table(
        self,
        old_table_name: TableName,
        new_table_name: TableName,
    ) -> None:
        # The function that renames tables in MSSQL takes string literals as arguments instead of identifiers,
        # so we shouldn't quote the identifiers.
        self.execute(exp.rename_table(old_table_name, new_table_name), quote_identifiers=False)

    def _insert_overwrite_by_condition(
        self,
        table_name: TableName,
        source_queries: t.List[SourceQuery],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        where: t.Optional[exp.Condition] = None,
        insert_overwrite_strategy_override: t.Optional[InsertOverwriteStrategy] = None,
        **kwargs: t.Any,
    ) -> None:
        # note that this is passed as table_properties here rather than physical_properties
        use_merge_strategy = kwargs.get("table_properties", {}).get("mssql_merge_exists")
        if (not where or where == exp.true()) and not use_merge_strategy:
            # this is a full table replacement, call the base strategy to do DELETE+INSERT
            # which will result in TRUNCATE+INSERT due to how we have overridden self.delete_from()
            return EngineAdapter._insert_overwrite_by_condition(
                self,
                table_name=table_name,
                source_queries=source_queries,
                target_columns_to_types=target_columns_to_types,
                where=where,
                insert_overwrite_strategy_override=InsertOverwriteStrategy.DELETE_INSERT,
                **kwargs,
            )

        # For conditional overwrites or when mssql_merge_exists is set use MERGE
        return super()._insert_overwrite_by_condition(
            table_name=table_name,
            source_queries=source_queries,
            target_columns_to_types=target_columns_to_types,
            where=where,
            insert_overwrite_strategy_override=insert_overwrite_strategy_override,
            **kwargs,
        )

    def delete_from(self, table_name: TableName, where: t.Union[str, exp.Expr]) -> None:
        if where == exp.true():
            # "A TRUNCATE TABLE operation can be rolled back within a transaction."
            # ref: https://learn.microsoft.com/en-us/sql/t-sql/statements/truncate-table-transact-sql?view=sql-server-ver15#remarks
            return self.execute(
                exp.TruncateTable(expressions=[exp.to_table(table_name, dialect=self.dialect)])
            )

        return super().delete_from(table_name, where)
