from __future__ import annotations

import logging
import typing as t
from functools import partial

from sqlglot import exp

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.mixins import (
    GetCurrentCatalogFromFunctionMixin,
    HiveMetastoreTablePropertiesMixin,
    RowDiffMixin,
)
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
    InsertOverwriteStrategy,
    SourceQuery,
    set_catalog,
)
from sqlmesh.utils import classproperty, get_source_columns_to_types
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    import pandas as pd
    from pyspark.sql import types as spark_types

    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter._typing import (
        DF,
        PySparkDataFrame,
        PySparkSession,
        Query,
    )
    from sqlmesh.core.engine_adapter.base import QueryOrDF
    from sqlmesh.engines.spark.db_api.spark_session import SparkSessionConnection


logger = logging.getLogger(__name__)


@set_catalog()
class SparkEngineAdapter(
    GetCurrentCatalogFromFunctionMixin, HiveMetastoreTablePropertiesMixin, RowDiffMixin
):
    DIALECT = "spark"
    SUPPORTS_TRANSACTIONS = False
    INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.INSERT_OVERWRITE
    COMMENT_CREATION_TABLE = CommentCreationTable.IN_SCHEMA_DEF_NO_CTAS
    COMMENT_CREATION_VIEW = CommentCreationView.IN_SCHEMA_DEF_NO_COMMANDS
    # Note: Some formats (like Delta and Iceberg) support REPLACE TABLE but since we don't
    # currently check for storage formats we say we don't support REPLACE TABLE
    SUPPORTS_REPLACE_TABLE = False
    QUOTE_IDENTIFIERS_IN_VIEWS = False
    SUPPORTED_DROP_CASCADE_OBJECT_KINDS = ["DATABASE", "SCHEMA"]

    WAP_PREFIX = "wap_"
    BRANCH_PREFIX = "branch_"
    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            # default decimal precision varies across backends
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(), (0,)],
        },
    }

    @property
    def connection(self) -> SparkSessionConnection:
        return self._connection_pool.get()

    @property
    def spark(self) -> PySparkSession:
        return self.connection.spark

    @property
    def _use_spark_session(self) -> bool:
        return True

    @property
    def use_serverless(self) -> bool:
        return False

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.FULL_SUPPORT

    @classproperty
    def _sqlglot_to_spark_primitive_mapping(self) -> t.Dict[t.Any, t.Any]:
        from pyspark.sql import types as spark_types

        return {
            exp.DataType.Type.TINYINT: spark_types.ByteType,
            exp.DataType.Type.SMALLINT: spark_types.ShortType,
            exp.DataType.Type.INT: spark_types.IntegerType,
            exp.DataType.Type.BIGINT: spark_types.LongType,
            exp.DataType.Type.FLOAT: spark_types.FloatType,
            exp.DataType.Type.DOUBLE: spark_types.DoubleType,
            exp.DataType.Type.DECIMAL: spark_types.DecimalType,
            exp.DataType.Type.VARCHAR: spark_types.StringType,
            exp.DataType.Type.CHAR: spark_types.StringType,
            exp.DataType.Type.TEXT: spark_types.StringType,
            exp.DataType.Type.BINARY: spark_types.BinaryType,
            exp.DataType.Type.BOOLEAN: spark_types.BooleanType,
            exp.DataType.Type.DATE: spark_types.DateType,
            exp.DataType.Type.TIMESTAMPNTZ: spark_types.TimestampNTZType,
            exp.DataType.Type.DATETIME: spark_types.TimestampNTZType,
            exp.DataType.Type.TIMESTAMPLTZ: spark_types.TimestampType,
            exp.DataType.Type.TIMESTAMP: spark_types.TimestampType,
            exp.DataType.Type.TIMESTAMPTZ: spark_types.TimestampType,
        }

    @classproperty
    def _sqlglot_to_spark_complex_mapping(self) -> t.Dict[t.Any, t.Any]:
        from pyspark.sql import types as spark_types

        return {
            exp.DataType.Type.ARRAY: spark_types.ArrayType,
            exp.DataType.Type.MAP: spark_types.MapType,
            exp.DataType.Type.STRUCT: spark_types.StructType,
        }

    @classproperty
    def _spark_to_sqlglot_primitive_mapping(self) -> t.Dict[t.Any, t.Any]:
        return {v: k for k, v in self._sqlglot_to_spark_primitive_mapping.items()}

    @classproperty
    def _spark_to_sqlglot_complex_mapping(self) -> t.Dict[t.Any, t.Any]:
        return {v: k for k, v in self._sqlglot_to_spark_complex_mapping.items()}

    @classmethod
    def spark_to_sqlglot_types(cls, input: spark_types.StructType) -> t.Dict[str, exp.DataType]:
        from pyspark.sql import types as spark_types

        def spark_complex_to_sqlglot_complex(
            complex_type: t.Union[
                spark_types.StructType, spark_types.ArrayType, spark_types.MapType
            ],
        ) -> exp.DataType:
            def get_fields(
                complex_type: t.Union[
                    spark_types.StructType, spark_types.ArrayType, spark_types.MapType
                ],
            ) -> t.Sequence[spark_types.DataType]:
                if isinstance(complex_type, spark_types.StructType):
                    return complex_type.fields
                if isinstance(complex_type, spark_types.ArrayType):
                    return [complex_type.elementType]
                if isinstance(complex_type, spark_types.MapType):
                    return [complex_type.keyType, complex_type.valueType]
                raise SQLMeshError(f"Unsupported complex type: {complex_type}")

            expressions: t.List[t.Union[exp.ColumnDef, exp.DataType]] = []
            fields = get_fields(complex_type)
            for field in fields:
                if isinstance(field, (spark_types.StructType, spark_types.MapType)):
                    expressions.append(spark_complex_to_sqlglot_complex(field))
                elif isinstance(field, spark_types.StructField):
                    sqlglot_data_type = cls._spark_to_sqlglot_primitive_mapping.get(
                        type(field.dataType)
                    ) or spark_complex_to_sqlglot_complex(
                        field.dataType  # type: ignore
                    )
                    kind = (
                        sqlglot_data_type
                        if isinstance(sqlglot_data_type, exp.DataType)
                        else exp.DataType(this=sqlglot_data_type)
                    )
                    expressions.append(exp.ColumnDef(this=exp.to_identifier(field.name), kind=kind))
                else:
                    kind = exp.DataType(this=cls._spark_to_sqlglot_primitive_mapping[type(field)])
                    expressions.append(kind)
            dtype = cls._spark_to_sqlglot_complex_mapping[type(complex_type)]
            return exp.DataType(
                this=dtype,
                expressions=expressions,
                nested=True,
            )

        resp = spark_complex_to_sqlglot_complex(input)
        return {column_def.this.name: column_def.args["kind"] for column_def in resp.expressions}

    @classmethod
    def sqlglot_to_spark_types(cls, input: t.Dict[str, exp.DataType]) -> spark_types.StructType:
        from pyspark.sql import types as spark_types

        def sqlglot_complex_to_spark_complex(complex_type: exp.DataType) -> spark_types.DataType:
            is_struct = complex_type.is_type(exp.DataType.Type.STRUCT)
            expressions = []
            for column_def in complex_type.expressions:
                col_name = column_def.this.name if is_struct else None
                data_type = column_def.args["kind"] if is_struct else column_def
                primitive_func = cls._sqlglot_to_spark_primitive_mapping.get(data_type.this)
                type_func = (
                    primitive_func
                    if primitive_func
                    else partial(sqlglot_complex_to_spark_complex, data_type)
                )
                if is_struct:
                    expressions.append(spark_types.StructField(col_name, type_func()))  # type: ignore
                else:
                    expressions.append(type_func())  # type: ignore
            klass = cls._sqlglot_to_spark_complex_mapping[complex_type.this]
            if is_struct:
                return klass(expressions)
            return klass(*expressions)

        return t.cast(
            spark_types.StructType,
            sqlglot_complex_to_spark_complex(
                exp.DataType(
                    this=exp.DataType.Type.STRUCT,
                    expressions=[
                        exp.ColumnDef(this=exp.to_identifier(column), kind=data_type)
                        for column, data_type in input.items()
                    ],
                )
            ),
        )

    @classmethod
    def is_pyspark_df(cls, value: t.Any) -> bool:
        return hasattr(value, "sparkSession")

    @classmethod
    def try_get_pyspark_df(cls, value: t.Any) -> t.Optional[PySparkDataFrame]:
        if cls.is_pyspark_df(value):
            return value
        return None

    @classmethod
    def try_get_pandas_df(cls, value: t.Any) -> t.Optional[pd.DataFrame]:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            return value
        return None

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
        if target_columns_to_types:
            return target_columns_to_types, list(source_columns or target_columns_to_types)
        if self.is_pyspark_df(query_or_df):
            from pyspark.sql import DataFrame

            target_columns_to_types = self.spark_to_sqlglot_types(
                t.cast(DataFrame, query_or_df).schema
            )
            return target_columns_to_types, list(source_columns or target_columns_to_types)
        return super()._columns_to_types(
            query_or_df, target_columns_to_types, source_columns=source_columns
        )

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        df = self._ensure_pyspark_df(df, target_columns_to_types, source_columns=source_columns)

        def query_factory() -> Query:
            temp_table = self._get_temp_table(target_table or "spark", table_only=True)
            df.createOrReplaceGlobalTempView(temp_table.sql(dialect=self.dialect))  # type: ignore
            temp_table.set("db", "global_temp")
            return exp.select(*self._select_columns(target_columns_to_types)).from_(temp_table)

        return [SourceQuery(query_factory=query_factory)]

    def _ensure_pyspark_df(
        self,
        generic_df: DF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> PySparkDataFrame:
        pyspark_df = self.try_get_pyspark_df(generic_df)
        if not pyspark_df:
            df = self.try_get_pandas_df(generic_df)
            if df is None:
                raise SQLMeshError(
                    "Ensure PySpark DF can only be run on a PySpark or Pandas DataFrame"
                )

            if target_columns_to_types:
                source_columns_to_types = get_source_columns_to_types(
                    target_columns_to_types, source_columns
                )
                # ensure Pandas dataframe column order matches columns_to_types
                df = df[list(source_columns_to_types)]
            else:
                source_columns_to_types = None
            kwargs = (
                dict(schema=self.sqlglot_to_spark_types(source_columns_to_types))
                if source_columns_to_types
                else {}
            )
            pyspark_df = self.spark.createDataFrame(df, **kwargs)  # type: ignore
        if target_columns_to_types:
            select_columns = self._casted_columns(
                target_columns_to_types, source_columns=source_columns
            )
            pyspark_df = pyspark_df.selectExpr(*[x.sql(self.dialect) for x in select_columns])  # type: ignore
        return pyspark_df

    def _get_temp_table(
        self, table: TableName, table_only: bool = False, quoted: bool = True
    ) -> exp.Table:
        """
        Returns the name of the temp table that should be used for the given table name.
        """
        table = super()._get_temp_table(table, table_only=table_only)
        table_name_id = table.args["this"]
        # Spark with local filesystem has an issue with temp tables that start with __temp so
        # we update here to remove the leading double underscore
        table_name_id.set("this", table_name_id.this.replace("__temp_", "temp_"))
        return table

    def fetchdf(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> pd.DataFrame:
        return self.fetch_pyspark_df(query, quote_identifiers=quote_identifiers).toPandas()

    def fetch_pyspark_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> PySparkDataFrame:
        return self._ensure_pyspark_df(
            self._fetch_native_df(query, quote_identifiers=quote_identifiers)
        )

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        schema_name = to_schema(schema_name).sql(dialect=self.dialect)
        pattern = "*" if object_names is None else "|".join(object_names)
        sql = f"SHOW TABLE EXTENDED IN {schema_name} LIKE '{pattern}'"
        try:
            results = (
                self.fetch_pyspark_df(sql).collect()
                if self._use_spark_session
                else self.fetchdf(sql).to_dict("records")
            )
        # Improvement: Figure out all the different exceptions we could get from executing a query either with or
        # without a Spark Session. In addition Databricks would need to be updated to handle it's own exceptions.
        # Therefore just doing except Exception for now.
        except Exception:
            return []
        data_objects = []
        catalog = self.get_current_catalog()
        for row in results:  # type: ignore
            row_dict = row.asDict() if not isinstance(row, dict) else row
            if row_dict.get("isTemporary"):
                continue
            schema = row_dict.get("namespace") or row_dict.get("database")
            data_objects.append(
                DataObject(
                    catalog=catalog,
                    schema=schema,
                    name=row_dict["tableName"],
                    type=(
                        DataObjectType.VIEW
                        if "Type: VIEW" in row_dict["information"]
                        else DataObjectType.TABLE
                    ),
                )
            )
        return data_objects

    def get_current_catalog(self) -> t.Optional[str]:
        if self._use_spark_session:
            return self.connection.get_current_catalog()
        return super().get_current_catalog()

    def set_current_catalog(self, catalog_name: str) -> None:
        self.connection.set_current_catalog(catalog_name)

    def _get_current_schema(self) -> str:
        if self._use_spark_session:
            return self.spark.catalog.currentDatabase()
        return self.fetchone(exp.select(exp.func("current_database")))[0]  # type: ignore

    def get_data_object(
        self, target_name: TableName, safe_to_cache: bool = False
    ) -> t.Optional[DataObject]:
        target_table = exp.to_table(target_name)
        if isinstance(target_table.this, exp.Dot) and target_table.this.expression.name.startswith(
            f"{self.BRANCH_PREFIX}{self.WAP_PREFIX}"
        ):
            # Exclude the branch name
            target_table.set("this", target_table.this.this)
        return super().get_data_object(target_table, safe_to_cache=safe_to_cache)

    def create_state_table(
        self,
        table_name: str,
        target_columns_to_types: t.Dict[str, exp.DataType],
        primary_key: t.Optional[t.Tuple[str, ...]] = None,
    ) -> None:
        self.create_table(
            table_name,
            target_columns_to_types,
            partitioned_by=[exp.column(x) for x in primary_key] if primary_key else None,
        )

    def _native_df_to_pandas_df(
        self,
        query_or_df: QueryOrDF,
    ) -> t.Union[Query, pd.DataFrame]:
        if pyspark_df := self.try_get_pyspark_df(query_or_df):
            return pyspark_df.toPandas()

        return super()._native_df_to_pandas_df(query_or_df)

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
        table_name = (
            table_name_or_schema.this
            if isinstance(table_name_or_schema, exp.Schema)
            else exp.to_table(table_name_or_schema)
        )
        # Spark doesn't support creating a wap table DDL. Therefore we check if this is a wap table and if it is,
        # this is not a replace, and the table already exists then we can safely just return. Otherwise we let it error.
        if not expression and isinstance(table_name.this, exp.Dot):
            wap_id = table_name.this.parts[-1].name
            if wap_id.startswith(f"{self.BRANCH_PREFIX}{self.WAP_PREFIX}"):
                table_name.set("this", table_name.this.this)

        do_dummy_insert = False
        if self.wap_enabled:
            wap_supported = (
                kwargs.get("storage_format") or ""
            ).lower() == "iceberg" or self.wap_supported(table_name)
            do_dummy_insert = (
                False if not wap_supported or not exists else not self.table_exists(table_name)
            )
        super()._create_table(
            table_name_or_schema,
            expression,
            exists=exists,
            replace=replace,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            column_descriptions=column_descriptions,
            track_rows_processed=track_rows_processed,
            **kwargs,
        )
        table_name = (
            table_name_or_schema.this
            if isinstance(table_name_or_schema, exp.Schema)
            else exp.to_table(table_name_or_schema)
        )
        if do_dummy_insert:
            # Performing a dummy insert to create a dummy snapshot for Iceberg tables
            # to workaround https://github.com/apache/iceberg/issues/8849.
            dummy_insert = exp.insert(exp.select("*").from_(table_name), table_name)
            self.execute(dummy_insert)

    def wap_supported(self, table_name: TableName) -> bool:
        fqn = self._ensure_fqn(table_name)
        return (
            self.spark.conf.get(f"spark.sql.catalog.{fqn.catalog}")
            == "org.apache.iceberg.spark.SparkCatalog"
        )

    def wap_table_name(self, table_name: TableName, wap_id: str) -> str:
        branch_name = self._wap_branch_name(wap_id)
        fqn = self._ensure_fqn(table_name)
        return exp.Dot.build([fqn, exp.to_identifier(f"{self.BRANCH_PREFIX}{branch_name}")]).sql(
            dialect=self.dialect
        )

    def wap_prepare(self, table_name: TableName, wap_id: str) -> str:
        branch_name = self._wap_branch_name(wap_id)
        fqn = self._ensure_fqn(table_name)
        self.execute(f"ALTER TABLE {fqn.sql(dialect=self.dialect)} CREATE BRANCH {branch_name}")
        return self.wap_table_name(table_name, wap_id)

    def wap_publish(self, table_name: TableName, wap_id: str) -> None:
        branch_name = self._wap_branch_name(wap_id)
        fqn = self._ensure_fqn(table_name)

        get_snapshot_id_query = (
            exp.select("snapshot_id")
            .from_(exp.Dot.build([fqn, exp.to_identifier("refs")]))
            .where(exp.column("name").eq(branch_name))
        )
        iceberg_snapshot_ids = self.fetchall(get_snapshot_id_query)
        if not iceberg_snapshot_ids:
            raise SQLMeshError(f"Could not find Iceberg branch '{branch_name}'.")
        iceberg_snapshot_id = iceberg_snapshot_ids[0][0]

        logger.info(
            "Cherry-picking Iceberg snapshot %s into table '%s'...", iceberg_snapshot_id, fqn
        )

        self.execute(
            f"CALL {fqn.catalog}.system.cherrypick_snapshot('{fqn.db}.{fqn.name}', {iceberg_snapshot_id})"
        )
        self.execute(f"ALTER TABLE {fqn.sql(dialect=self.dialect)} DROP BRANCH {branch_name}")

    def _ensure_fqn(self, table_name: TableName) -> exp.Table:
        if isinstance(table_name, exp.Table):
            table_name = table_name.copy()
        table = exp.to_table(table_name, dialect=self.dialect)
        if not table.catalog:
            table.set("catalog", self.get_current_catalog())
        if not table.db:
            table.set("db", self._get_current_schema())
        return table

    def _build_create_comment_column_exp(
        self, table: exp.Table, column_name: str, column_comment: str, table_kind: str = "TABLE"
    ) -> exp.Comment | str:
        table_sql = table.sql(dialect=self.dialect, identify=True)
        column_sql = exp.column(column_name).sql(dialect=self.dialect, identify=True)

        truncated_comment = self._truncate_column_comment(column_comment)
        comment_sql = exp.Literal.string(truncated_comment).sql(dialect=self.dialect)

        return f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} COMMENT {comment_sql}"

    @classmethod
    def _wap_branch_name(cls, wap_id: str) -> str:
        return f"{cls.WAP_PREFIX}{wap_id}"
