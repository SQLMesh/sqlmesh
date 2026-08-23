from __future__ import annotations

import contextlib
import logging
import typing as t

from sqlglot import exp
from sqlglot.helper import ensure_list
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify_columns import quote_identifiers

import sqlmesh.core.constants as c
from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.mixins import (
    GetCurrentCatalogFromFunctionMixin,
    ClusteredByMixin,
    RowDiffMixin,
    GrantsFromInfoSchemaMixin,
)
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    DataObject,
    DataObjectType,
    SourceQuery,
    set_catalog,
)
from sqlmesh.utils import optional_import, get_source_columns_to_types
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.pandas import columns_to_types_from_dtypes

logger = logging.getLogger(__name__)
snowpark = optional_import("snowflake.snowpark")

if t.TYPE_CHECKING:
    import pandas as pd

    from sqlmesh.core._typing import SchemaName, SessionProperties, TableName
    from sqlmesh.core.engine_adapter._typing import (
        DF,
        Query,
        QueryOrDF,
        SnowparkSession,
    )
    from sqlmesh.core.node import IntervalUnit


@set_catalog(
    override_mapping={
        "_get_data_objects": CatalogSupport.REQUIRES_SET_CATALOG,
        "create_schema": CatalogSupport.REQUIRES_SET_CATALOG,
        "drop_schema": CatalogSupport.REQUIRES_SET_CATALOG,
        "drop_catalog": CatalogSupport.REQUIRES_SET_CATALOG,  # needs a catalog to issue a query to information_schema.databases even though the result is global
    }
)
class SnowflakeEngineAdapter(
    GetCurrentCatalogFromFunctionMixin, ClusteredByMixin, RowDiffMixin, GrantsFromInfoSchemaMixin
):
    DIALECT = "snowflake"
    SUPPORTS_MATERIALIZED_VIEWS = True
    SUPPORTS_MATERIALIZED_VIEW_SCHEMA = True
    SUPPORTS_CLONING = True
    SUPPORTS_MANAGED_MODELS = True
    CURRENT_CATALOG_EXPRESSION = exp.func("current_database")
    SUPPORTS_CREATE_DROP_CATALOG = True
    SUPPORTS_METADATA_TABLE_LAST_MODIFIED_TS = True
    SUPPORTED_DROP_CASCADE_OBJECT_KINDS = ["DATABASE", "SCHEMA", "TABLE"]
    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            exp.DataType.build("BINARY", dialect=DIALECT).this: [(8388608,)],
            exp.DataType.build("VARBINARY", dialect=DIALECT).this: [(8388608,)],
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(38, 0), (0,)],
            exp.DataType.build("CHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("NCHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: [(16777216,)],
            exp.DataType.build("TIME", dialect=DIALECT).this: [(9,)],
            exp.DataType.build("TIMESTAMP", dialect=DIALECT).this: [(9,)],
            exp.DataType.build("TIMESTAMP_LTZ", dialect=DIALECT).this: [(9,)],
            exp.DataType.build("TIMESTAMP_NTZ", dialect=DIALECT).this: [(9,)],
            exp.DataType.build("TIMESTAMP_TZ", dialect=DIALECT).this: [(9,)],
        },
    }
    MANAGED_TABLE_KIND = "DYNAMIC TABLE"
    SNOWPARK = "snowpark"
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    SUPPORTS_GRANTS = True
    CURRENT_USER_OR_ROLE_EXPRESSION: exp.Expr = exp.func("CURRENT_ROLE")
    USE_CATALOG_IN_GRANTS = True

    @contextlib.contextmanager
    def session(self, properties: SessionProperties) -> t.Iterator[None]:
        warehouse = properties.get("warehouse")
        if not warehouse:
            yield
            return

        if isinstance(warehouse, str):
            warehouse = exp.to_identifier(warehouse)
        if not isinstance(warehouse, exp.Expr):
            raise SQLMeshError(f"Invalid warehouse: '{warehouse}'")

        warehouse_exp = quote_identifiers(
            normalize_identifiers(warehouse, dialect=self.dialect), dialect=self.dialect
        )
        warehouse_sql = warehouse_exp.sql(dialect=self.dialect)
        current_warehouse_sql = self._current_warehouse.sql(dialect=self.dialect)

        if warehouse_sql == current_warehouse_sql:
            yield
            return

        self.execute(f"USE WAREHOUSE {warehouse_sql}")
        try:
            yield
        finally:
            self.execute(f"USE WAREHOUSE {current_warehouse_sql}")

    @property
    def _current_warehouse(self) -> exp.Identifier:
        current_warehouse_str = self.fetchone("SELECT CURRENT_WAREHOUSE()")[0]  # type: ignore
        # The warehouse value returned by Snowflake is already normalized, so only quoting is needed.
        return quote_identifiers(exp.to_identifier(current_warehouse_str), dialect=self.dialect)

    @property
    def snowpark(self) -> t.Optional[SnowparkSession]:
        if snowpark:
            if not self._connection_pool.get_attribute(self.SNOWPARK):
                # Snowpark sessions are not thread safe so we create a session per thread to prevent them from interfering with each other
                # The sessions are cleaned up when close() is called
                new_session = snowpark.Session.builder.configs(
                    {"connection": self._connection_pool.get()}
                ).create()
                self._connection_pool.set_attribute(self.SNOWPARK, new_session)

            return self._connection_pool.get_attribute(self.SNOWPARK)

        return None

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.FULL_SUPPORT

    @staticmethod
    def _grant_object_kind(table_type: DataObjectType) -> str:
        if table_type == DataObjectType.VIEW:
            return "VIEW"
        if table_type == DataObjectType.MATERIALIZED_VIEW:
            return "MATERIALIZED VIEW"
        if table_type == DataObjectType.MANAGED_TABLE:
            return "DYNAMIC TABLE"
        return "TABLE"

    def _get_current_schema(self) -> str:
        """Returns the current default schema for the connection."""
        result = self.fetchone("SELECT CURRENT_SCHEMA()")
        if not result or not result[0]:
            raise SQLMeshError("Unable to determine current schema")
        return str(result[0])

    def _create_catalog(self, catalog_name: exp.Identifier) -> None:
        props = exp.Properties(
            expressions=[exp.SchemaCommentProperty(this=exp.Literal.string(c.SQLMESH_MANAGED))]
        )
        self.execute(
            exp.Create(
                this=exp.Table(this=catalog_name), kind="DATABASE", exists=True, properties=props
            )
        )

    def _drop_catalog(self, catalog_name: exp.Identifier) -> None:
        # only drop the catalog if it was created by SQLMesh, which is indicated by its comment matching {c.SQLMESH_MANAGED}
        exists_check = (
            exp.select(exp.Literal.number(1))
            .from_(exp.to_table("information_schema.databases"))
            .where(
                exp.and_(
                    exp.column("database_name").eq(exp.Literal.string(catalog_name)),
                    exp.column("comment").eq(exp.Literal.string(c.SQLMESH_MANAGED)),
                )
            )
        )
        normalize_identifiers(exists_check, dialect=self.dialect)
        if self.fetchone(exists_check, quote_identifiers=True) is not None:
            self.execute(exp.Drop(this=exp.Table(this=catalog_name), kind="DATABASE", exists=True))
        else:
            logger.warning(
                f"Not dropping database {catalog_name.sql(dialect=self.dialect)} because there is no indication it is '{c.SQLMESH_MANAGED}'"
            )

    def _create_table(
        self,
        table_name_or_schema: t.Union[exp.Schema, TableName],
        expression: t.Optional[exp.Expr],
        exists: bool = True,
        replace: bool = False,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        table_kind: t.Optional[str] = None,
        track_rows_processed: bool = True,
        **kwargs: t.Any,
    ) -> None:
        table_format = kwargs.get("table_format")
        if table_format and isinstance(table_format, str):
            table_format = table_format.upper()
            if not table_kind:
                table_kind = f"{table_format} TABLE"
            elif table_kind == self.MANAGED_TABLE_KIND:
                table_kind = f"DYNAMIC {table_format} TABLE"

        super()._create_table(
            table_name_or_schema=table_name_or_schema,
            expression=expression,
            exists=exists,
            replace=replace,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            column_descriptions=column_descriptions,
            table_kind=table_kind,
            track_rows_processed=False,  # snowflake tracks CTAS row counts incorrectly
            **kwargs,
        )

    def create_managed_table(
        self,
        table_name: TableName,
        query: Query,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        partitioned_by: t.Optional[t.List[exp.Expr]] = None,
        clustered_by: t.Optional[t.List[exp.Expr]] = None,
        table_properties: t.Optional[t.Dict[str, exp.Expr]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        target_table = exp.to_table(table_name)

        # Snowflake defaults to uppercase and it also makes the property presence checks
        # easier
        table_properties = {k.upper(): v for k, v in (table_properties or {}).items()}

        # the WAREHOUSE property is required for a Dynamic Table
        if "WAREHOUSE" not in table_properties:
            table_properties["WAREHOUSE"] = self._current_warehouse

        # so is TARGET_LAG
        # ref: https://docs.snowflake.com/en/sql-reference/sql/create-dynamic-table
        if "TARGET_LAG" not in table_properties:
            raise SQLMeshError(
                "`target_lag` must be specified in the model physical_properties for a Snowflake Dynamic Table"
            )

        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query, target_columns_to_types, target_table=target_table, source_columns=source_columns
        )

        self._create_table_from_source_queries(
            target_table,
            source_queries,
            target_columns_to_types,
            replace=self.SUPPORTS_REPLACE_TABLE,
            partitioned_by=partitioned_by,
            clustered_by=clustered_by,
            table_properties=table_properties,
            table_description=table_description,
            column_descriptions=column_descriptions,
            table_kind=self.MANAGED_TABLE_KIND,
            **kwargs,
        )

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
        properties = create_kwargs.pop("properties", None)
        if not properties:
            properties = exp.Properties(expressions=[])
        if replace:
            properties.append("expressions", exp.CopyGrantsProperty())

        super().create_view(
            view_name=view_name,
            query_or_df=query_or_df,
            target_columns_to_types=target_columns_to_types,
            replace=replace,
            materialized=materialized,
            materialized_properties=materialized_properties,
            table_description=table_description,
            column_descriptions=column_descriptions,
            view_properties=view_properties,
            properties=properties,
            source_columns=source_columns,
            **create_kwargs,
        )

    def drop_managed_table(self, table_name: TableName, exists: bool = True) -> None:
        self._drop_object(table_name, exists, kind=self.MANAGED_TABLE_KIND)

    def _build_table_properties_exp(
        self,
        catalog_name: t.Optional[str] = None,
        table_format: t.Optional[str] = None,
        storage_format: t.Optional[str] = None,
        partitioned_by: t.Optional[t.List[exp.Expr]] = None,
        partition_interval_unit: t.Optional[IntervalUnit] = None,
        clustered_by: t.Optional[t.List[exp.Expr]] = None,
        table_properties: t.Optional[t.Dict[str, exp.Expr]] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        table_kind: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        properties: t.List[exp.Expr] = []

        # TODO: there is some overlap with the base class and other engine adapters
        # we need a better way of filtering table properties relevent to the current engine
        # and using those to build the expression
        if table_description:
            properties.append(
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        if (
            clustered_by
            and (clustered_by_prop := self._build_clustered_by_exp(clustered_by)) is not None
        ):
            properties.append(clustered_by_prop)

        if table_properties:
            table_properties = {k.upper(): v for k, v in table_properties.items()}
            # if we are creating a non-dynamic table; remove any properties that are only valid for dynamic tables
            # this is necessary because we create "normal" tables from the same managed model definition for dev previews and the "normal" tables dont support these parameters
            if "DYNAMIC" not in (table_kind or "").upper():
                for prop in {"WAREHOUSE", "TARGET_LAG", "REFRESH_MODE", "INITIALIZE"}:
                    table_properties.pop(prop, None)

            table_type = self._pop_creatable_type_from_properties(table_properties)
            properties.extend(ensure_list(table_type))

            properties.extend(self._table_or_view_properties_to_expressions(table_properties))

        return exp.Properties(expressions=properties) if properties else None

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        import pandas as pd
        from pandas.api.types import is_datetime64_any_dtype

        source_columns_to_types = get_source_columns_to_types(
            target_columns_to_types, source_columns
        )

        temp_table = self._get_temp_table(
            target_table or "pandas", quoted=False
        )  # write_pandas() re-quotes everything without checking if its already quoted

        is_snowpark_dataframe = snowpark and isinstance(df, snowpark.dataframe.DataFrame)

        def query_factory() -> Query:
            # The catalog needs to be normalized before being passed to Snowflake's library functions because they
            # just wrap whatever they are given in quotes without checking if its already quoted
            database = (
                normalize_identifiers(temp_table.catalog, dialect=self.dialect)
                if temp_table.catalog
                else None
            )

            if is_snowpark_dataframe:
                temp_table.set("catalog", database)

                # only quote columns if they arent already quoted
                # if the Snowpark dataframe was created from a Pandas dataframe via snowpark.create_dataframe(pandas_df),
                # then they will be quoted already. But if the Snowpark dataframe was created manually by the user, then the
                # columns may not be quoted
                columns_already_quoted = all(
                    col.startswith('"') and col.endswith('"') for col in df.columns
                )
                local_df = df
                if not columns_already_quoted:
                    local_df = df.rename(
                        {
                            col: exp.to_identifier(col).sql(dialect=self.dialect, identify=True)
                            for col in source_columns_to_types
                        }
                    )  # type: ignore
                local_df.createOrReplaceTempView(
                    temp_table.sql(dialect=self.dialect, identify=True)
                )  # type: ignore
            elif isinstance(df, pd.DataFrame):
                from snowflake.connector.pandas_tools import write_pandas

                ordered_df = df[list(source_columns_to_types)].reset_index(drop=True)

                # Workaround for https://github.com/snowflakedb/snowflake-connector-python/issues/1034
                # The above issue has already been fixed upstream, but we keep the following
                # line anyway in order to support a wider range of Snowflake versions.
                schema = temp_table.db
                if temp_table.catalog:
                    schema = f"{temp_table.catalog}.{schema}"
                self.set_current_schema(schema)

                # See: https://stackoverflow.com/a/75627721
                for column, kind in source_columns_to_types.items():
                    if is_datetime64_any_dtype(ordered_df.dtypes[column]):
                        if kind.is_type("date"):  # type: ignore
                            ordered_df[column] = pd.to_datetime(ordered_df[column]).dt.date  # type: ignore
                        elif getattr(ordered_df.dtypes[column], "tz", None) is not None:  # type: ignore
                            ordered_df[column] = pd.to_datetime(ordered_df[column]).dt.strftime(
                                "%Y-%m-%d %H:%M:%S.%f%z"
                            )  # type: ignore
                        # https://github.com/snowflakedb/snowflake-connector-python/issues/1677
                        else:  # type: ignore
                            ordered_df[column] = pd.to_datetime(ordered_df[column]).dt.strftime(
                                "%Y-%m-%d %H:%M:%S.%f"
                            )  # type: ignore

                # create the table first using our usual method ensure the column datatypes match what we parsed with sqlglot
                # otherwise we would be trusting `write_pandas()` from the snowflake lib to do this correctly
                self.create_table(temp_table, source_columns_to_types, table_kind="TEMPORARY TABLE")

                write_pandas(
                    self._connection_pool.get(),
                    ordered_df,
                    temp_table.name,
                    schema=temp_table.db or None,
                    database=database.sql(dialect=self.dialect) if database else None,
                    chunk_size=self.DEFAULT_BATCH_SIZE,
                    overwrite=True,
                    table_type="temp",
                )
            else:
                raise SQLMeshError(
                    f"Unknown dataframe type: {type(df)} for {target_table}. Expecting pandas or snowpark."
                )

            return exp.select(
                *self._casted_columns(target_columns_to_types, source_columns=source_columns)
            ).from_(temp_table)

        def cleanup() -> None:
            if is_snowpark_dataframe:
                if hasattr(df, "table_name"):
                    if isinstance(df.table_name, str):
                        # created by the Snowpark library if the Snowpark DataFrame was created from a Pandas DataFrame
                        # (if the Snowpark DataFrame was created via native means then there is no 'table_name' property and no temp table)
                        self.drop_table(df.table_name)
                self.drop_view(temp_table)
            else:
                self.drop_table(temp_table)

        # the cleanup_func technically isnt needed because the temp table gets dropped when the session ends
        # but boy does it make our multi-adapter integration tests easier to write
        return [SourceQuery(query_factory=query_factory, cleanup_func=cleanup)]

    def _fetch_native_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> DF:
        import pandas as pd
        from snowflake.connector.errors import NotSupportedError

        self.execute(query, quote_identifiers=quote_identifiers)

        try:
            return self.cursor.fetch_pandas_all()
        except NotSupportedError:
            # Sometimes Snowflake will not return results as an Arrow result and the fetch from
            # pandas will fail (Ex: `SHOW TERSE OBJECTS IN SCHEMA`). Therefore we manually convert
            # the result into a DataFrame when this happens.
            rows = self.cursor.fetchall()
            columns = self.cursor._result_set.batches[0].column_names
            return pd.DataFrame([dict(zip(columns, row)) for row in rows])

    def _native_df_to_pandas_df(
        self,
        query_or_df: QueryOrDF,
    ) -> t.Union[Query, pd.DataFrame]:
        if snowpark and isinstance(query_or_df, snowpark.DataFrame):
            return query_or_df.to_pandas()

        return super()._native_df_to_pandas_df(query_or_df)

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and optionally catalog.
        """

        schema = to_schema(schema_name)
        catalog_name = schema.catalog or self.get_current_catalog()

        query = (
            exp.select(
                exp.column("TABLE_CATALOG").as_("catalog"),
                exp.column("TABLE_NAME").as_("name"),
                exp.column("TABLE_SCHEMA").as_("schema_name"),
                exp.case()
                .when(
                    exp.And(
                        this=exp.column("TABLE_TYPE").eq("BASE TABLE"),
                        expression=exp.column("IS_DYNAMIC").eq("YES"),
                    ),
                    exp.Literal.string("MANAGED_TABLE"),
                )
                .when(exp.column("TABLE_TYPE").eq("BASE TABLE"), exp.Literal.string("TABLE"))
                .when(exp.column("TABLE_TYPE").eq("TEMPORARY TABLE"), exp.Literal.string("TABLE"))
                .when(exp.column("TABLE_TYPE").eq("LOCAL TEMPORARY"), exp.Literal.string("TABLE"))
                .when(exp.column("TABLE_TYPE").eq("EXTERNAL TABLE"), exp.Literal.string("TABLE"))
                .when(exp.column("TABLE_TYPE").eq("EVENT TABLE"), exp.Literal.string("TABLE"))
                .when(exp.column("TABLE_TYPE").eq("VIEW"), exp.Literal.string("VIEW"))
                .when(
                    exp.column("TABLE_TYPE").eq("MATERIALIZED VIEW"),
                    exp.Literal.string("MATERIALIZED_VIEW"),
                )
                .else_(exp.column("TABLE_TYPE"))
                .as_("type"),
                exp.column("CLUSTERING_KEY").as_("clustering_key"),
            )
            .from_(exp.table_("TABLES", db="INFORMATION_SCHEMA", catalog=catalog_name))
            .where(exp.column("TABLE_SCHEMA").eq(schema.db))
            # Snowflake seems to have delayed internal metadata updates and will sometimes return duplicates
            .distinct()
        )
        if object_names:
            query = query.where(exp.column("TABLE_NAME").isin(*object_names))

        # exclude SNOWPARK_TEMP_TABLE tables that are managed by the Snowpark library and are an implementation
        # detail of dealing with DataFrame's
        query = query.where(exp.column("TABLE_NAME").like("SNOWPARK_TEMP_TABLE%").not_())

        df = self.fetchdf(query, quote_identifiers=True)
        if df.empty:
            return []
        return [
            DataObject(
                catalog=row.catalog,  # type: ignore
                schema=row.schema_name,  # type: ignore
                name=row.name,  # type: ignore
                type=DataObjectType.from_str(row.type),  # type: ignore
                clustering_key=row.clustering_key,  # type: ignore
            )
            # lowercase the column names for cases where Snowflake might return uppercase column names for certain catalogs
            for row in df.rename(columns={col: col.lower() for col in df.columns}).itertuples()
        ]

    def _get_grant_expression(self, table: exp.Table) -> exp.Expr:
        # Upon execute the catalog in table expressions are properly normalized to handle the case where a user provides
        # the default catalog in their connection config. This doesn't though update catalogs in strings like when querying
        # the information schema. So we need to manually replace those here.
        expression = super()._get_grant_expression(table)
        for col_exp in expression.find_all(exp.Column):
            if col_exp.this.name == "table_catalog":
                and_exp = col_exp.parent
                assert and_exp is not None, "Expected column expression to have a parent"
                assert and_exp.expression, "Expected AND expression to have an expression"
                normalized_catalog = self._normalize_catalog(
                    exp.table_("placeholder", db="placeholder", catalog=and_exp.expression.this)
                )
                and_exp.set(
                    "expression",
                    exp.Literal.string(normalized_catalog.args["catalog"].alias_or_name),
                )
        return expression

    def set_current_catalog(self, catalog: str) -> None:
        self.execute(exp.Use(this=exp.to_identifier(catalog)))

    def set_current_schema(self, schema: str) -> None:
        self.execute(exp.Use(kind="SCHEMA", this=to_schema(schema)))

    def _normalize_catalog(self, expression: exp.Expr) -> exp.Expr:
        # note: important to use self._default_catalog instead of the self.default_catalog property
        # otherwise we get RecursionError: maximum recursion depth exceeded
        # because it calls get_current_catalog(), which executes a query, which needs the default catalog, which calls get_current_catalog()... etc
        if self._default_catalog:
            # the purpose of this function is to identify instances where the default catalog is being used
            # (so that we can replace it with the actual catalog as specified in the gateway)
            #
            # we can't do a direct string comparison because the catalog value on the model
            # gets changed when it's normalized as part of generating `model.fqn`
            def unquote_and_lower(identifier: str) -> str:
                return exp.parse_identifier(identifier).name.lower()

            default_catalog_unquoted = unquote_and_lower(self._default_catalog)
            default_catalog_normalized = normalize_identifiers(
                self._default_catalog, dialect=self.dialect
            )

            def catalog_rewriter(node: exp.Expr) -> exp.Expr:
                if isinstance(node, exp.Table):
                    if node.catalog:
                        # only replace the catalog on the model with the target catalog if the two are functionally equivalent
                        if unquote_and_lower(node.catalog) == default_catalog_unquoted:
                            node.set("catalog", default_catalog_normalized)
                elif isinstance(node, exp.Use) and isinstance(node.this, exp.Identifier):
                    if unquote_and_lower(node.this.output_name) == default_catalog_unquoted:
                        node.set("this", default_catalog_normalized)
                return node

            # Rewrite whatever default catalog is present on the query to be compatible with what the user supplied in the
            # Snowflake connection config. This is because the catalog present on the model gets normalized and quoted to match
            # the source dialect, which isnt always compatible with Snowflake
            expression = expression.transform(catalog_rewriter)
        return expression

    def _to_sql(self, expression: exp.Expr, quote: bool = True, **kwargs: t.Any) -> str:
        return super()._to_sql(
            expression=self._normalize_catalog(expression), quote=quote, **kwargs
        )

    def _create_column_comments(
        self,
        table_name: TableName,
        column_comments: t.Dict[str, str],
        table_kind: str = "TABLE",
        materialized_view: bool = False,
    ) -> None:
        """
        Reference: https://docs.snowflake.com/en/sql-reference/sql/alter-table-column#syntax
        """
        if not column_comments:
            return

        table = exp.to_table(table_name)
        table_sql = self._to_sql(table)

        list_comment_sql = []
        for column_name, column_comment in column_comments.items():
            column_sql = exp.column(column_name).sql(dialect=self.dialect, identify=True)

            truncated_comment = self._truncate_column_comment(column_comment)
            comment_sql = exp.Literal.string(truncated_comment).sql(dialect=self.dialect)

            list_comment_sql.append(f"COLUMN {column_sql} COMMENT {comment_sql}")

        combined_sql = f"ALTER {table_kind} {table_sql} ALTER {', '.join(list_comment_sql)}"
        try:
            self.execute(combined_sql)
        except Exception:
            logger.warning(
                f"Column comments for table '{table.alias_or_name}' not registered - this may be due to limited permissions.",
                exc_info=True,
            )

    def clone_table(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        replace: bool = False,
        exists: bool = True,
        clone_kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        **kwargs: t.Any,
    ) -> None:
        # The Snowflake adapter should use the transient property to clone transient tables
        if physical_properties := kwargs.get("rendered_physical_properties"):
            table_type = self._pop_creatable_type_from_properties(physical_properties)
            if isinstance(table_type, exp.TransientProperty):
                kwargs["properties"] = exp.Properties(expressions=[table_type])

        super().clone_table(
            target_table_name,
            source_table_name,
            replace=replace,
            clone_kwargs=clone_kwargs,
            **kwargs,
        )

    @t.overload
    def _columns_to_types(
        self,
        query_or_df: DF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.Tuple[t.Dict[str, exp.DataType], t.List[str]]: ...

    @t.overload
    def _columns_to_types(
        self,
        query_or_df: Query,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.Tuple[t.Optional[t.Dict[str, exp.DataType]], t.Optional[t.List[str]]]: ...

    def _columns_to_types(
        self,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.Tuple[t.Optional[t.Dict[str, exp.DataType]], t.Optional[t.List[str]]]:
        if not target_columns_to_types and snowpark and isinstance(query_or_df, snowpark.DataFrame):
            target_columns_to_types = columns_to_types_from_dtypes(
                query_or_df.sample(n=1).to_pandas().dtypes.items()
            )
            return target_columns_to_types, list(source_columns or target_columns_to_types)

        return super()._columns_to_types(
            query_or_df, target_columns_to_types, source_columns=source_columns
        )

    def close(self) -> t.Any:
        if snowpark_session := self._connection_pool.get_attribute(self.SNOWPARK):
            snowpark_session.close()  # type: ignore
            self._connection_pool.set_attribute(self.SNOWPARK, None)

        return super().close()

    def get_table_last_modified_ts(self, table_names: t.List[TableName]) -> t.List[int]:
        from sqlmesh.utils.date import to_timestamp

        num_tables = len(table_names)

        query = "SELECT LAST_ALTERED FROM INFORMATION_SCHEMA.TABLES WHERE"
        for i, table_name in enumerate(table_names):
            table = exp.to_table(table_name)
            query += f"""(TABLE_NAME = '{table.name}' AND TABLE_SCHEMA = '{table.db}' AND TABLE_CATALOG = '{table.catalog}')"""
            if i < num_tables - 1:
                query += " OR "

        result = self.fetchall(query)
        return [to_timestamp(row[0]) for row in result]
