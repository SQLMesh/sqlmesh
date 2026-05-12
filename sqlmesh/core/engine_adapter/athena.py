from __future__ import annotations
from functools import lru_cache
import typing as t
import logging
from sqlglot import exp
from sqlmesh.core.dialect import to_schema
from sqlmesh.utils.aws import validate_s3_uri, parse_s3_uri
from sqlmesh.core.engine_adapter.mixins import PandasNativeFetchDFSupportMixin, RowDiffMixin
from sqlmesh.core.engine_adapter.trino import TrinoEngineAdapter
from sqlmesh.core.node import IntervalUnit
import posixpath
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    DataObject,
    DataObjectType,
    CommentCreationTable,
    CommentCreationView,
    SourceQuery,
    InsertOverwriteStrategy,
)
if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter._typing import QueryOrDF

    TableType = t.Union[t.Literal["hive"], t.Literal["iceberg"]]

logger = logging.getLogger(__name__)


class AthenaEngineAdapter(PandasNativeFetchDFSupportMixin, RowDiffMixin):
    DIALECT = "athena"
    SUPPORTS_TRANSACTIONS = False
    SUPPORTS_REPLACE_TABLE = False
    # Athena's support for table and column comments is too patchy to consider "supported"
    # Hive tables: Table + Column comments are supported
    # Iceberg tables: Column comments only
    # CTAS, Views: No comment support at all
    COMMENT_CREATION_TABLE = CommentCreationTable.UNSUPPORTED
    COMMENT_CREATION_VIEW = CommentCreationView.UNSUPPORTED
    SCHEMA_DIFFER_KWARGS = TrinoEngineAdapter.SCHEMA_DIFFER_KWARGS
    MAX_TIMESTAMP_PRECISION = 3  # copied from Trino
    # Athena does not deal with comments well, e.g:
    # >>> self._execute('/* test */ DESCRIBE foo')
    #     pyathena.error.OperationalError: FAILED: ParseException line 1:0 cannot recognize input near '/' '*' 'test'
    ATTACH_CORRELATION_ID = False
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    SUPPORTED_DROP_CASCADE_OBJECT_KINDS = ["DATABASE", "SCHEMA"]

    def __init__(
        self, *args: t.Any, s3_warehouse_location: t.Optional[str] = None, **kwargs: t.Any
    ):
        # Need to pass s3_warehouse_location to the superclass so that it goes into _extra_config
        # which means that EngineAdapter.with_settings() keeps this property when it makes a clone
        super().__init__(*args, s3_warehouse_location=s3_warehouse_location, **kwargs)
        self.s3_warehouse_location = s3_warehouse_location

        self._default_catalog = self._default_catalog or "awsdatacatalog"

    @property
    def s3_warehouse_location(self) -> t.Optional[str]:
        return self._s3_warehouse_location

    @s3_warehouse_location.setter
    def s3_warehouse_location(self, value: t.Optional[str]) -> None:
        if value:
            value = validate_s3_uri(value, base=True)
        self._s3_warehouse_location = value

    @property
    def s3_warehouse_location_or_raise(self) -> str:
        # this makes tests easier to write without extra null checks to keep mypy happy
        if location := self.s3_warehouse_location:
            return location

        raise SQLMeshError("s3_warehouse_location was expected to be populated; it isnt")

    @property
    def catalog_support(self) -> CatalogSupport:
        # Athena supports querying and writing to multiple catalogs (e.g. awsdatacatalog and s3tablescatalog)
        # without needing a SET CATALOG command.
        return CatalogSupport.FULL_SUPPORT

    def create_state_table(
        self,
        table_name: str,
        target_columns_to_types: t.Dict[str, exp.DataType],
        primary_key: t.Optional[t.Tuple[str, ...]] = None,
    ) -> None:
        self.create_table(
            table_name,
            target_columns_to_types,
            primary_key=primary_key,
            # it's painfully slow, but it works
            table_format="iceberg",
        )

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and optionally catalog.
        """
        schema_name = to_schema(schema_name)
        schema = schema_name.db
        catalog = schema_name.catalog

        # In Athena, information_schema queries spanning catalogs often fail with CATALOG_NOT_FOUND.
        # We need to temporarily set the default catalog to the target catalog to execute this query successfully
        # or use system views depending on exact driver support. By omitting the catalog from the table explicitly
        # and setting it via connection, we ensure it maps to the correct AWS/S3 integration natively.
        info_schema_tables = exp.table_("tables", db="information_schema", alias="t")
        
        query = (
            exp.select(
                exp.column("table_catalog").as_("catalog"),
                exp.column("table_schema", table="t").as_("schema"),
                exp.column("table_name", table="t").as_("name"),
                exp.case()
                .when(
                    exp.column("table_type", table="t").eq("BASE TABLE"),
                    exp.Literal.string("table"),
                )
                .else_(exp.column("table_type", table="t"))
                .as_("type"),
            )
            .from_(info_schema_tables)
            .where(exp.column("table_schema", table="t").eq(schema))
        )
        if object_names:
            query = query.where(exp.column("table_name", table="t").isin(*object_names))

        current_catalog = self.get_current_catalog()
        
        if catalog and catalog != self._default_catalog:
            if current_catalog != catalog:
                self.set_current_catalog(catalog)
        
        try:
            df = self.fetchdf(query)
            
            # For queries that don't return the catalog in the result (some drivers/engines), 
            # fill it in if it's missing or empty and we explicitly queried for a specific catalog
            if catalog and df is not None and not df.empty and "catalog" in df.columns:
                df["catalog"] = df["catalog"].fillna(catalog)
                # Replace empty strings with the catalog as well
                df["catalog"] = df["catalog"].replace("", catalog)
                
        finally:
            if catalog and catalog != self._default_catalog and current_catalog is not None and current_catalog != catalog:
                self.set_current_catalog(current_catalog)

        return [
            DataObject(
                catalog=row.catalog,  # type: ignore
                schema=row.schema,  # type: ignore
                name=row.name,  # type: ignore
                type=DataObjectType.from_str(row.type),  # type: ignore
            )
            for row in df.itertuples()
        ]

    def table_exists(self, table_name: TableName) -> bool:
        from sqlmesh.core.engine_adapter.base import _get_data_object_cache_key
        table = exp.to_table(table_name)
        data_object_cache_key = _get_data_object_cache_key(table.catalog, table.db, table.name)
        
        if data_object_cache_key in self._data_object_cache:
            logger.debug("Table existence cache hit: %s", data_object_cache_key)
            return self._data_object_cache[data_object_cache_key] is not None

        try:
            # We don't use DESCRIBE because it fails with "Unsupported ddl with 2 catalogs"
            # for cross-catalog queries in Athena.
            # And since table_exists isn't run with the set_catalog decorator (which sets QueryExecutionContext),
            # we must fallback to a query that works with fully qualified names or 
            # uses the information_schema/limit 0. A limit 0 select works with fully qualified names in Athena.
            self.execute(exp.select("1").from_(table).limit(0))
            return True
        except Exception:
            return False

    def columns(
        self, table_name: TableName, include_pseudo_columns: bool = False
    ) -> t.Dict[str, exp.DataType]:
        table = exp.to_table(table_name)
        # note: the data_type column contains the full parameterized type, eg 'varchar(10)'
        
        catalog = table.catalog

        # Fetching column info across catalogs often fails in Athena (CATALOG_NOT_FOUND)
        # So we strip the catalog and set the current catalog dynamically
        info_schema_columns = exp.table_("columns", db="information_schema")
        
        query = (
            exp.select("column_name", "data_type")
            .from_(info_schema_columns)
            .where(exp.column("table_schema").eq(table.db), exp.column("table_name").eq(table.name))
            .order_by("ordinal_position")
        )
        
        current_catalog = self.get_current_catalog()
        
        if catalog and catalog != self._default_catalog:
            if current_catalog != catalog:
                self.set_current_catalog(catalog)
        
        try:
            result = self.fetchdf(query, quote_identifiers=True)
            return {
                str(r.column_name): exp.DataType.build(str(r.data_type))
                for r in result.itertuples(index=False)
            }
        except Exception as e:
            # If information_schema query fails, we fallback to DESCRIBE.
            # But DESCRIBE with multiple catalogs fails in Athena, so we strip the catalog here
            # and rely on the set_current_catalog mechanism (applied at the EngineAdapter method level)
            # to set the catalog in the execution context.
            describe_table = table.copy()
            if catalog and catalog != self._default_catalog:
                describe_table.set("catalog", None)
            
            try:
                self.execute(exp.Describe(this=describe_table, kind="TABLE"))
                
                from sqlmesh.core.engine_adapter.base import _decoded_str
                import itertools
                describe_output = self.cursor.fetchall()
                return {
                    # Note: MySQL  returns the column type as bytes.
                    column_name: exp.DataType.build(_decoded_str(column_type), dialect=self.dialect)
                    for column_name, column_type, *_ in itertools.takewhile(
                        lambda t: not t[0].startswith("#"),
                        describe_output,
                    )
                    if column_name and column_name.strip() and column_type and column_type.strip()
                }
            finally:
                pass # context reset is handled in outer finally block
        finally:
            if catalog and catalog != self._default_catalog and current_catalog is not None and current_catalog != catalog:
                self.set_current_catalog(current_catalog)

    def _drop_object(
        self,
        name: TableName | SchemaName,
        exists: bool = True,
        kind: str = "TABLE",
        cascade: bool = False,
        **drop_args: t.Any,
    ) -> None:
        if cascade and kind.upper() in self.SUPPORTED_DROP_CASCADE_OBJECT_KINDS:
            drop_args["cascade"] = cascade

        target_table = exp.to_table(name).copy()
        is_schema = kind.upper() == "SCHEMA"
        catalog = target_table.db if is_schema else target_table.catalog
        
        if catalog and catalog != self._default_catalog:
            if is_schema:
                target_table.set("db", None)
            else:
                target_table.set("catalog", None)
                
            current_catalog = self.get_current_catalog()
            if current_catalog != catalog:
                self.set_current_catalog(catalog)
            
            try:
                self.execute(exp.Drop(this=target_table, kind=kind, exists=exists, **drop_args))
            finally:
                if current_catalog is not None and current_catalog != catalog:
                    self.set_current_catalog(current_catalog)
        else:
            self.execute(exp.Drop(this=target_table, kind=kind, exists=exists, **drop_args))
            
        self._clear_data_object_cache(name)

    def _create_schema(
        self,
        schema_name: SchemaName,
        ignore_if_exists: bool,
        warn_on_error: bool,
        properties: t.List[exp.Expr],
        kind: str,
    ) -> None:
        schema = to_schema(schema_name)

        if location := self._table_location(table_properties=None, table=exp.to_table(schema_name)):
            # don't add extra LocationProperty's if one already exists
            if not any(p for p in properties if isinstance(p, exp.LocationProperty)):
                properties.append(location)

        if schema.catalog:
            target_schema = schema.copy()
            catalog = target_schema.catalog
            target_schema.set("catalog", None)
            
            current_catalog = self.get_current_catalog()
            if current_catalog != catalog:
                self.set_current_catalog(catalog)
            
            try:
                self.execute(
                    exp.Create(
                        this=target_schema,
                        kind=kind,
                        exists=ignore_if_exists,
                        properties=exp.Properties(expressions=properties),
                    )
                )
            except Exception as e:
                if not warn_on_error:
                    raise
                logger.warning("Failed to create %s '%s': %s", kind.lower(), schema_name, e)
            finally:
                if current_catalog is not None and current_catalog != catalog:
                    self.set_current_catalog(current_catalog)
            return

        return super()._create_schema(
            schema_name=schema_name,
            ignore_if_exists=ignore_if_exists,
            warn_on_error=warn_on_error,
            properties=properties,
            kind=kind,
        )

    def _get_temp_table(
        self, table: TableName, table_only: bool = False, quoted: bool = True
    ) -> exp.Table:
        """
        Returns the name of the temp table that should be used for the given table name.
        """
        from sqlmesh.utils import random_id
        
        table = t.cast(exp.Table, exp.to_table(table).copy())
        
        # AWS S3 Tables (and Athena generally) prefer or require table names to start with a letter.
        # S3 Tables specifically fail with: "The specified table name is not valid" if it starts with __temp_
        table.set(
            "this", exp.to_identifier(f"temp_{table.name}_{random_id(short=True)}", quoted=quoted)
        )

        if table_only:
            table.set("db", None)
            table.set("catalog", None)

        return table

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
        table: exp.Table
        if isinstance(table_name_or_schema, str):
            table = exp.to_table(table_name_or_schema)
        elif isinstance(table_name_or_schema, exp.Schema):
            table = table_name_or_schema.this
        else:
            table = table_name_or_schema

        catalog = table.catalog
        current_catalog = self.get_current_catalog()
        
        # For non-CTAS CREATE TABLE in a non-default catalog, the catalog is stripped by _build_create_table_exp.
        # We need to set the query execution context here.
        if not expression and catalog and catalog != self._default_catalog:
            if current_catalog != catalog:
                self.set_current_catalog(catalog)
        
        try:
            super()._create_table(
                table_name_or_schema=table_name_or_schema,
                expression=expression,
                exists=exists,
                replace=replace,
                target_columns_to_types=target_columns_to_types,
                table_description=table_description,
                column_descriptions=column_descriptions,
                table_kind=table_kind,
                track_rows_processed=track_rows_processed,
                **kwargs,
            )
        finally:
            if not expression and catalog and catalog != self._default_catalog:
                if current_catalog is not None and current_catalog != catalog:
                    self.set_current_catalog(current_catalog)

    def _build_create_table_exp(
        self,
        table_name_or_schema: t.Union[exp.Schema, TableName],
        expression: t.Optional[exp.Expr],
        exists: bool = True,
        replace: bool = False,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        table_kind: t.Optional[str] = None,
        partitioned_by: t.Optional[t.List[exp.Expr]] = None,
        table_properties: t.Optional[t.Dict[str, exp.Expr]] = None,
        **kwargs: t.Any,
    ) -> exp.Create:
        exists = False if replace else exists

        table: exp.Table
        if isinstance(table_name_or_schema, str):
            table = exp.to_table(table_name_or_schema)
        elif isinstance(table_name_or_schema, exp.Schema):
            table = table_name_or_schema.this
        else:
            table = table_name_or_schema

        table_format = kwargs.pop("table_format", None)
        if not table_format and table_properties and "table_format" in table_properties:
            tf = table_properties.get("table_format")
            table_format = tf.name if isinstance(tf, exp.Literal) else str(tf)

        properties = self._build_table_properties_exp(
            table=table,
            expression=expression,
            target_columns_to_types=target_columns_to_types,
            partitioned_by=partitioned_by,
            table_properties=table_properties,
            table_description=table_description,
            table_kind=table_kind,
            table_format=table_format,
            **kwargs,
        )

        is_hive = self._table_type(table_format) == "hive"

        # Filter any PARTITIONED BY properties from the main column list since they cant be specified in both places
        # ref: https://docs.aws.amazon.com/athena/latest/ug/partitions.html
        if is_hive and partitioned_by and isinstance(table_name_or_schema, exp.Schema):
            partitioned_by_column_names = {e.name for e in partitioned_by}
            filtered_expressions = [
                e
                for e in table_name_or_schema.expressions
                if isinstance(e, exp.ColumnDef) and e.this.name not in partitioned_by_column_names
            ]
            table_name_or_schema.args["expressions"] = filtered_expressions

        create_table = table_name_or_schema.copy()
        
        # When creating a table without AS SELECT, Athena fails with "Unsupported ddl with 2 catalogs"
        # if a custom catalog like s3tablescatalog/supply is provided in the CREATE TABLE statement.
        # It requires the catalog to be provided via QueryExecutionContext instead.
        # The set_catalog decorator (which calls set_current_catalog) passes it to the QueryExecutionContext.
        # But we also need to strip it from the generated CREATE TABLE statement.
        # Note: We must strip the catalog from the table in the schema if table_name_or_schema is a schema.
        target_table = create_table.this if isinstance(create_table, exp.Schema) else create_table
        if not expression and target_table.catalog:
            target_table.set("catalog", None)

        return exp.Create(
            this=create_table,
            kind=table_kind or "TABLE",
            replace=replace,
            exists=exists,
            expression=expression,
            properties=properties,
        )

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
        table: t.Optional[exp.Table] = None,
        expression: t.Optional[exp.Expr] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        properties: t.List[exp.Expr] = []
        table_properties = table_properties.copy() if table_properties else {}

        s3_table_prop = table_properties.pop("s3_table", None)
        is_s3_table = False
        if s3_table_prop is not None:
            if isinstance(s3_table_prop, exp.Boolean):
                is_s3_table = s3_table_prop.this
            elif isinstance(s3_table_prop, exp.Literal):
                is_s3_table = s3_table_prop.name.lower() in ("true", "1")
            else:
                is_s3_table = str(s3_table_prop).lower() in ("true", "1")
        elif table and table.catalog and table.catalog.startswith("s3tablescatalog/"):
            is_s3_table = True

        tf = table_properties.pop("table_format", None)
        if not table_format and tf:
            table_format = tf.name if isinstance(tf, exp.Literal) else str(tf)

        is_hive = self._table_type(table_format) == "hive"
        is_iceberg = not is_hive

        if is_s3_table and is_hive:
            raise SQLMeshError("Amazon S3 Tables only support the Iceberg format")

        if is_hive and not expression:
            # Hive tables are CREATE EXTERNAL TABLE, Iceberg tables are CREATE TABLE
            # Unless it's a CTAS, those are always CREATE TABLE
            properties.append(exp.ExternalProperty())

        if table_format and not is_s3_table:
            properties.append(
                exp.Property(this=exp.var("table_type"), value=exp.Literal.string(table_format))
            )

        if table_description:
            properties.append(exp.SchemaCommentProperty(this=exp.Literal.string(table_description)))

        if partitioned_by:
            schema_expressions: t.List[exp.Expr] = []
            if is_hive and target_columns_to_types:
                # For Hive-style tables, you cannot include the partitioned by columns in the main set of columns
                # In the PARTITIONED BY expression, you also cant just include the column names, you need to include the data type as well
                # ref: https://docs.aws.amazon.com/athena/latest/ug/partitions.html
                for match_name, match_dtype in self._find_matching_columns(
                    partitioned_by, target_columns_to_types
                ):
                    column_def = exp.ColumnDef(this=exp.to_identifier(match_name), kind=match_dtype)
                    schema_expressions.append(column_def)
            else:
                schema_expressions = partitioned_by

            if is_hive:
                properties.append(
                    exp.PartitionedByProperty(this=exp.Schema(expressions=schema_expressions))
                )
            else:
                if is_s3_table and expression:
                    array_exprs = []
                    for e in schema_expressions:
                        e_copy = e.copy()
                        e_copy.transform(
                            lambda n: n.name if isinstance(n, exp.Identifier) else n, copy=False
                        )
                        expr_sql = e_copy.sql(dialect="athena")
                        array_exprs.append(exp.Literal.string(expr_sql))

                    properties.append(
                        exp.Property(
                            this=exp.var("partitioning"), value=exp.Array(expressions=array_exprs)
                        )
                    )
                else:
                    properties.append(
                        exp.PartitionedByProperty(this=exp.Schema(expressions=schema_expressions))
                    )

        if clustered_by:
            # Athena itself supports CLUSTERED BY, via the syntax CLUSTERED BY (col) INTO <n> BUCKETS
            # However, SQLMesh is more closely aligned with BigQuery's notion of clustering and
            # defines `clustered_by` as a List[str] with no way of indicating the number of buckets
            #
            # Athena's concept of CLUSTER BY is more like Iceberg's `bucket(<num_buckets>, col)` partition transform
            logging.warning("clustered_by is not supported in the Athena adapter at this time")

        if storage_format:
            if is_iceberg:
                if not is_s3_table or storage_format.lower() == "parquet":
                    # TBLPROPERTIES('format'='parquet')
                    table_properties["format"] = exp.Literal.string(storage_format)
                elif is_s3_table and storage_format.lower() != "parquet":
                    raise SQLMeshError("Amazon S3 Tables only support the PARQUET storage format")
            else:
                # STORED AS PARQUET
                properties.append(exp.FileFormatProperty(this=storage_format))

        if table and not is_s3_table and (location := self._table_location_or_raise(table_properties, table)):
            properties.append(location)

            if is_iceberg and expression:
                # To make a CTAS expression persist as iceberg, alongside setting `table_type=iceberg`, you also need to set is_external=false
                # Note that SQLGlot does the right thing with LocationProperty and writes it as `location` (Iceberg) instead of `external_location` (Hive)
                # ref: https://docs.aws.amazon.com/athena/latest/ug/create-table-as.html#ctas-table-properties
                properties.append(exp.Property(this=exp.var("is_external"), value="false"))

        if not is_s3_table:
            for name, value in table_properties.items():
                properties.append(exp.Property(this=exp.var(name), value=value))
        elif is_s3_table:
            # According to AWS documentation for S3 Tables CTAS queries:
            # "The `table_type` property defaults to `ICEBERG`, so you don't need to explicitly specify it"
            # "If you don't specify a format, the system automatically uses `PARQUET`"
            # We explicitly prevent all TBLPROPERTIES because Athena doesn't support them during CTAS
            if expression:
                # the only property allowed in CTAS for S3 Tables is 'format' (which we captured above)
                format_val = table_properties.pop("format", exp.Literal.string("PARQUET"))
                # Ensure it's uppercase PARQUET for S3 Tables just to be safe as per AWS examples
                if isinstance(format_val, exp.Literal) and format_val.name.lower() == "parquet":
                    format_val = exp.Literal.string("PARQUET")
                properties.append(exp.Property(this=exp.var("format"), value=format_val))

                if table_properties:
                    logging.warning(f"Ignoring unsupported table properties for S3 Table CTAS: {list(table_properties.keys())}")
            else:
                # Standard CREATE TABLE for S3 Tables allows properties
                for name, value in table_properties.items():
                    properties.append(exp.Property(this=exp.var(name), value=value))

        if properties:
            return exp.Properties(expressions=properties)

        return None

    def drop_table(self, table_name: TableName, exists: bool = True, **kwargs: t.Any) -> None:
        table = exp.to_table(table_name)

        if self._query_table_type(table) == "hive":
            self._truncate_table(table)

        return super().drop_table(table_name=table, exists=exists, **kwargs)

    def _truncate_table(self, table_name: TableName) -> None:
        table = exp.to_table(table_name)

        # Truncating an Iceberg table is just DELETE FROM <table>
        if self._query_table_type(table) == "iceberg":
            return self.delete_from(table, exp.true())

        # Truncating a partitioned Hive table is dropping all partitions and deleting the data from S3
        if self._is_hive_partitioned_table(table):
            self._clear_partition_data(table, exp.true())
        elif s3_location := self._query_table_s3_location(table):
            # Truncating a non-partitioned Hive table is clearing out all data in its Location
            self._clear_s3_location(s3_location)

    def _table_type(self, table_format: t.Optional[str] = None) -> TableType:
        """
        Interpret the "table_format" property to check if this is a Hive or an Iceberg table
        """
        if table_format and table_format.lower() == "iceberg":
            return "iceberg"

        # if we cant detect any indication of Iceberg, this is a Hive table
        return "hive"

    def _query_table_type(self, table: exp.Table) -> t.Optional[TableType]:
        if self.table_exists(table):
            return self._query_table_type_or_raise(table)
        return None

    @lru_cache()
    def _query_table_type_or_raise(self, table: exp.Table) -> TableType:
        """
        Hit the DB to check if this is a Hive or an Iceberg table.

        Note that in order to @lru_cache() this method, we have the following assumptions:
         - The table must exist (otherwise we would cache None if this method was called before table creation and always return None after creation)
         - The table type will not change within the same SQLMesh session
        """
        # Note: SHOW TBLPROPERTIES gets parsed by SQLGlot as an exp.Command anyway so we just use a string here
        # This also means we need to use dialect="hive" instead of dialect="athena" so that the identifiers get the correct quoting (backticks)
        target_table = table.copy()
        catalog = target_table.catalog
        
        current_catalog = self.get_current_catalog()
        if catalog and catalog != self._default_catalog:
            target_table.set("catalog", None)
            if current_catalog != catalog:
                self.set_current_catalog(catalog)
        
        try:
            for row in self.fetchall(f"SHOW TBLPROPERTIES {target_table.sql(dialect='hive', identify=True)}"):
                # This query returns a single column with values like 'EXTERNAL\tTRUE'
                row_lower = row[0].lower()
                if "external" in row_lower and "true" in row_lower:
                    return "hive"
        except Exception:
            # If SHOW TBLPROPERTIES fails (e.g. S3 Tables might not support it), assume iceberg
            # S3 tables are always iceberg anyway
            pass
        finally:
            if catalog and catalog != self._default_catalog and current_catalog is not None and current_catalog != catalog:
                self.set_current_catalog(current_catalog)
        
        return "iceberg"

    def _is_hive_partitioned_table(self, table: exp.Table) -> bool:
        try:
            self._list_partitions(table=table, where=None, limit=1)
            return True
        except Exception as e:
            if "TABLE_NOT_FOUND" in str(e):
                return False
            raise e

    def _table_location_or_raise(
        self, table_properties: t.Optional[t.Dict[str, exp.Expr]], table: exp.Table
    ) -> exp.LocationProperty:
        location = self._table_location(table_properties, table)
        if not location:
            raise SQLMeshError(
                f"Cannot figure out location for table {table}. Please either set `s3_base_location` in `physical_properties` or set `s3_warehouse_location` in the Athena connection config"
            )
        return location

    def _table_location(
        self,
        table_properties: t.Optional[t.Dict[str, exp.Expr]],
        table: exp.Table,
    ) -> t.Optional[exp.LocationProperty]:
        base_uri: str

        # If the user has manually specified a `s3_base_location`, use it
        if table_properties and "s3_base_location" in table_properties:
            s3_base_location_property = table_properties.pop(
                "s3_base_location"
            )  # pop because it's handled differently and we dont want it to end up in the TBLPROPERTIES clause
            if isinstance(s3_base_location_property, exp.Expr):
                base_uri = s3_base_location_property.name
            else:
                base_uri = s3_base_location_property

        elif self.s3_warehouse_location:
            # If the user has set `s3_warehouse_location` in the connection config, the base URI is <s3_warehouse_location>/<catalog>/<schema>/
            base_uri = posixpath.join(
                self.s3_warehouse_location, table.catalog or "", table.db or ""
            )
        else:
            return None

        full_uri = validate_s3_uri(posixpath.join(base_uri, table.text("this") or ""), base=True)
        return exp.LocationProperty(this=exp.Literal.string(full_uri))

    def _find_matching_columns(
        self, partitioned_by: t.List[exp.Expr], columns_to_types: t.Dict[str, exp.DataType]
    ) -> t.List[t.Tuple[str, exp.DataType]]:
        matches = []
        for col in partitioned_by:
            # TODO: do we care about normalization?
            key = col.name
            if isinstance(col, exp.Column) and (match_dtype := columns_to_types.get(key)):
                matches.append((key, match_dtype))
        return matches

    def replace_query(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        supports_replace_table_override: t.Optional[bool] = None,
        **kwargs: t.Any,
    ) -> None:
        table = exp.to_table(table_name)

        if self._query_table_type(table=table) == "hive":
            self.drop_table(table)

        return super().replace_query(
            table_name=table,
            query_or_df=query_or_df,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            column_descriptions=column_descriptions,
            source_columns=source_columns,
            **kwargs,
        )

    def _insert_overwrite_by_time_partition(
        self,
        table_name: TableName,
        source_queries: t.List[SourceQuery],
        target_columns_to_types: t.Dict[str, exp.DataType],
        where: exp.Condition,
        **kwargs: t.Any,
    ) -> None:
        table = exp.to_table(table_name)

        table_type = self._query_table_type(table)

        if table_type == "iceberg":
            # Iceberg tables work as expected, we can use the default behaviour
            return super()._insert_overwrite_by_time_partition(
                table, source_queries, target_columns_to_types, where, **kwargs
            )

        # For Hive tables, we need to drop all the partitions covered by the query and delete the data from S3
        self._clear_partition_data(table, where)

        # Now the data is physically gone, we can continue with inserting a new partition
        return super()._insert_overwrite_by_time_partition(
            table,
            source_queries,
            target_columns_to_types,
            where,
            insert_overwrite_strategy_override=InsertOverwriteStrategy.INTO_IS_OVERWRITE,  # since we already cleared the data
            **kwargs,
        )

    def _clear_partition_data(self, table: exp.Table, where: t.Optional[exp.Condition]) -> None:
        if partitions_to_drop := self._list_partitions(table, where):
            for _, s3_location in partitions_to_drop:
                logger.debug(
                    f"Clearing S3 location for '{table.sql(dialect=self.dialect)}': {s3_location}"
                )
                self._clear_s3_location(s3_location)

            partition_values = [k for k, _ in partitions_to_drop]
            logger.debug(
                f"Dropping partitions for '{table.sql(dialect=self.dialect)}' from metastore: {partition_values}"
            )
            self._drop_partitions_from_metastore(table, partition_values)

    def _list_partitions(
        self,
        table: exp.Table,
        where: t.Optional[exp.Condition] = None,
        limit: t.Optional[int] = None,
    ) -> t.List[t.Tuple[t.List[str], str]]:
        # Use Athena's magic "$partitions" metadata table to identify the partitions to drop
        # Doing it this way allows us to use SQL to filter the partition list
        partition_table_name = table.copy()
        partition_table_name.this.replace(
            exp.to_identifier(f"{table.name}$partitions", quoted=True)
        )

        query = exp.select("*").from_(partition_table_name).where(where)
        if limit:
            query = query.limit(limit)

        partition_values = [list(r) for r in self.fetchall(query, quote_identifiers=True)]

        if partition_values:
            response = self._glue_client.batch_get_partition(
                DatabaseName=table.db,
                TableName=table.name,
                PartitionsToGet=[{"Values": [str(v) for v in lst]} for lst in partition_values],
            )
            return sorted(
                [(p["Values"], p["StorageDescriptor"]["Location"]) for p in response["Partitions"]]
            )

        return []

    def _query_table_s3_location(self, table: exp.Table) -> str:
        response = self._glue_client.get_table(DatabaseName=table.db, Name=table.name)

        # Athena wont let you create a table without a location, so *theoretically* this should never be empty
        if location := response.get("Table", {}).get("StorageDescriptor", {}).get("Location", None):
            return location

        raise SQLMeshError(f"Table {table} has no location set in the metastore!")

    def _drop_partitions_from_metastore(
        self, table: exp.Table, partition_values: t.List[t.List[str]]
    ) -> None:
        # todo: switch to itertools.batched when our minimum supported Python is 3.12
        # 25 = maximum number of partitions that batch_delete_partition can process at once
        # ref: https://docs.aws.amazon.com/glue/latest/webapi/API_BatchDeletePartition.html#API_BatchDeletePartition_RequestParameters
        def _chunks() -> t.Iterable[t.List[t.List[str]]]:
            for i in range(0, len(partition_values), 25):
                yield partition_values[i : i + 25]

        for batch in _chunks():
            self._glue_client.batch_delete_partition(
                DatabaseName=table.db,
                TableName=table.name,
                PartitionsToDelete=[{"Values": v} for v in batch],
            )

    def delete_from(self, table_name: TableName, where: t.Union[str, exp.Expr]) -> None:
        table = exp.to_table(table_name)

        table_type = self._query_table_type(table)

        # If Iceberg, DELETE operations work as expected
        if table_type == "iceberg":
            return super().delete_from(table, where)

        # If Hive, DELETE is an error
        if table_type == "hive":
            # However, if there are no actual records to delete, we can make DELETE a no-op
            # This simplifies a bunch of calling code that just assumes DELETE works (which to be fair is a reasonable assumption since it does for every other engine)
            empty_check = (
                exp.select("*").from_(table).where(where).limit(1)
            )  # deliberately not count(*) because we want the engine to stop as soon as it finds a record
            if len(self.fetchall(empty_check)) > 0:
                raise SQLMeshError("Cannot delete individual records from a Hive table")

        return None

    def _clear_s3_location(self, s3_uri: str) -> None:
        s3 = self._s3_client

        bucket, key = parse_s3_uri(s3_uri)
        if not key.endswith("/"):
            key = f"{key}/"

        keys_to_delete = []

        # note: uses Delimiter=/ to prevent stepping into folders
        # the assumption is that all the files in a partition live directly at the partition `Location`
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=key, Delimiter="/"
        ):
            # list_objects_v2() returns 1000 keys per page so that lines up nicely with delete_objects() being able to delete 1000 keys at a time
            keys = [item["Key"] for item in page.get("Contents", [])]
            if keys:
                keys_to_delete.append(keys)

        for chunk in keys_to_delete:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk]})

    @property
    def _glue_client(self) -> t.Any:
        return self._boto3_client("glue")

    @property
    def _s3_client(self) -> t.Any:
        return self._boto3_client("s3")

    def _boto3_client(self, name: str) -> t.Any:
        # use the client factory from PyAthena which is already configured with the correct AWS details
        conn = self.connection
        return conn.session.client(
            name,
            region_name=conn.region_name,
            config=conn.config,
            **conn._client_kwargs,
        )  # type: ignore

    def set_current_catalog(self, catalog: str) -> None:
        self.connection.catalog_name = catalog
        if hasattr(self.cursor, "_catalog_name"):
            self.cursor._catalog_name = catalog

    def get_current_catalog(self) -> t.Optional[str]:
        return self.connection.catalog_name
