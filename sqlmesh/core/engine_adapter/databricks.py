from __future__ import annotations

import logging
import typing as t
from functools import partial

from sqlglot import exp

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.mixins import GrantsFromInfoSchemaMixin
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    DataObject,
    DataObjectType,
    InsertOverwriteStrategy,
    SourceQuery,
)
from sqlmesh.core.engine_adapter.spark import SparkEngineAdapter
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.schema_diff import NestedSupport
from sqlmesh.engines.spark.db_api.spark_session import connection, SparkSessionConnection
from sqlmesh.utils.errors import SQLMeshError, MissingDefaultCatalogError

if t.TYPE_CHECKING:
    import pandas as pd

    from sqlmesh.core._typing import SchemaName, TableName, SessionProperties
    from sqlmesh.core.engine_adapter._typing import DF, PySparkSession, Query

logger = logging.getLogger(__name__)


class DatabricksEngineAdapter(SparkEngineAdapter, GrantsFromInfoSchemaMixin):
    DIALECT = "databricks"
    INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.REPLACE_WHERE
    SUPPORTS_CLONING = True
    SUPPORTS_MATERIALIZED_VIEWS = True
    SUPPORTS_MATERIALIZED_VIEW_SCHEMA = True
    SUPPORTS_GRANTS = True
    USE_CATALOG_IN_GRANTS = True
    # Spark has this set to false for compatibility when mixing with Trino but that isn't a concern with Databricks
    QUOTE_IDENTIFIERS_IN_VIEWS = True
    SCHEMA_DIFFER_KWARGS = {
        "support_positional_add": True,
        "nested_support": NestedSupport.ALL,
        "array_element_selector": "element",
        "parameterized_type_defaults": {
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(10, 0), (0,)],
        },
    }

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self._set_spark_engine_adapter_if_needed()

    @classmethod
    def can_access_spark_session(cls, disable_spark_session: bool) -> bool:
        from sqlmesh import RuntimeEnv

        if disable_spark_session:
            return False

        return RuntimeEnv.get().is_databricks

    @classmethod
    def can_access_databricks_connect(cls, disable_databricks_connect: bool) -> bool:
        if disable_databricks_connect:
            return False

        try:
            from databricks.connect import DatabricksSession  # noqa

            return True
        except ImportError:
            return False

    @property
    def _use_spark_session(self) -> bool:
        if self.can_access_spark_session(bool(self._extra_config.get("disable_spark_session"))):
            return True

        if self.can_access_databricks_connect(
            bool(self._extra_config.get("disable_databricks_connect"))
        ):
            if self._extra_config.get("databricks_connect_use_serverless"):
                return True

            if {
                "databricks_connect_cluster_id",
                "databricks_connect_server_hostname",
                "databricks_connect_access_token",
            }.issubset(self._extra_config):
                return True

        return False

    @property
    def is_spark_session_connection(self) -> bool:
        return isinstance(self.connection, SparkSessionConnection)

    def _set_spark_engine_adapter_if_needed(self) -> None:
        self._spark_engine_adapter = None

        if not self._use_spark_session or self.is_spark_session_connection:
            return

        from databricks.connect import DatabricksSession

        connect_kwargs = dict(
            host=self._extra_config["databricks_connect_server_hostname"],
            token=self._extra_config.get("databricks_connect_access_token"),
        )
        if "databricks_connect_use_serverless" in self._extra_config:
            connect_kwargs["serverless"] = True
        else:
            connect_kwargs["cluster_id"] = self._extra_config["databricks_connect_cluster_id"]

        catalog = self._extra_config.get("catalog")
        spark = (
            DatabricksSession.builder.remote(**connect_kwargs).userAgent("sqlmesh").getOrCreate()
        )
        self._spark_engine_adapter = SparkEngineAdapter(
            partial(connection, spark=spark, catalog=catalog),
            default_catalog=catalog,
            execute_log_level=self._execute_log_level,
            multithreaded=self._multithreaded,
            sql_gen_kwargs=self._sql_gen_kwargs,
            register_comments=self._register_comments,
            pre_ping=self._pre_ping,
            pretty_sql=self._pretty_sql,
        )

    @property
    def cursor(self) -> t.Any:
        if (
            self._connection_pool.get_attribute("use_spark_engine_adapter")
            and not self.is_spark_session_connection
        ):
            return self._spark_engine_adapter.cursor  # type: ignore
        return super().cursor

    @property
    def spark(self) -> PySparkSession:
        if not self._use_spark_session:
            raise SQLMeshError(
                "SparkSession is not available. "
                "Either run from a Databricks Notebook or "
                "install `databricks-connect` and configure it to connect to your Databricks cluster."
            )
        if self.is_spark_session_connection:
            return self.connection.spark
        return self._spark_engine_adapter.spark  # type: ignore

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.FULL_SUPPORT

    @staticmethod
    def _grant_object_kind(table_type: DataObjectType) -> str:
        if table_type == DataObjectType.VIEW:
            return "VIEW"
        if table_type == DataObjectType.MATERIALIZED_VIEW:
            return "MATERIALIZED VIEW"
        return "TABLE"

    def _get_grant_expression(self, table: exp.Table) -> exp.Expression:
        # We only care about explicitly granted privileges and not inherited ones
        # if this is removed you would see grants inherited from the catalog get returned
        expression = super()._get_grant_expression(table)
        expression.args["where"].set(
            "this",
            exp.and_(
                expression.args["where"].this,
                exp.column("inherited_from").eq(exp.Literal.string("NONE")),
                wrap=False,
            ),
        )
        return expression

    def _begin_session(self, properties: SessionProperties) -> t.Any:
        """Begin a new session."""
        # Align the different possible connectors to a single catalog
        self.set_current_catalog(self.default_catalog)  # type: ignore

    def _end_session(self) -> None:
        self._connection_pool.set_attribute("use_spark_engine_adapter", False)

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        if not self._use_spark_session:
            return super(SparkEngineAdapter, self)._df_to_source_queries(
                df, target_columns_to_types, batch_size, target_table, source_columns=source_columns
            )
        pyspark_df = self._ensure_pyspark_df(
            df, target_columns_to_types, source_columns=source_columns
        )

        def query_factory() -> Query:
            temp_table = self._get_temp_table(target_table or "spark", table_only=True)
            pyspark_df.createOrReplaceTempView(temp_table.sql(dialect=self.dialect))
            self._connection_pool.set_attribute("use_spark_engine_adapter", True)
            return exp.select(*self._select_columns(target_columns_to_types)).from_(temp_table)

        return [SourceQuery(query_factory=query_factory)]

    def _fetch_native_df(
        self, query: t.Union[exp.Expression, str], quote_identifiers: bool = False
    ) -> DF:
        """Fetches a DataFrame that can be either Pandas or PySpark from the cursor"""
        if self.is_spark_session_connection:
            return super()._fetch_native_df(query, quote_identifiers=quote_identifiers)
        if self._spark_engine_adapter:
            return self._spark_engine_adapter._fetch_native_df(  # type: ignore
                query, quote_identifiers=quote_identifiers
            )
        self.execute(query)
        return self.cursor.fetchall_arrow().to_pandas()

    def fetchdf(
        self, query: t.Union[exp.Expression, str], quote_identifiers: bool = False
    ) -> pd.DataFrame:
        """
        Returns a Pandas DataFrame from a query or expression.
        """
        import pandas as pd

        df = self._fetch_native_df(query, quote_identifiers=quote_identifiers)
        if not isinstance(df, pd.DataFrame):
            return df.toPandas()
        return df

    def get_current_catalog(self) -> t.Optional[str]:
        pyspark_catalog = None
        sql_connector_catalog = None
        if self._spark_engine_adapter:
            from py4j.protocol import Py4JError
            from pyspark.errors.exceptions.connect import SparkConnectGrpcException

            try:
                # Note: Spark 3.4+ Only API
                pyspark_catalog = self._spark_engine_adapter.get_current_catalog()
            except (Py4JError, SparkConnectGrpcException):
                pass
        elif self.is_spark_session_connection:
            pyspark_catalog = self.connection.spark.catalog.currentCatalog()
        if not self.is_spark_session_connection:
            result = self.fetchone(exp.select(self.CURRENT_CATALOG_EXPRESSION))
            sql_connector_catalog = result[0] if result else None
        if self._spark_engine_adapter and pyspark_catalog != sql_connector_catalog:
            logger.warning(
                f"Current catalog mismatch between Databricks SQL Connector and Databricks-Connect: `{sql_connector_catalog}` != `{pyspark_catalog}`. Set `catalog` connection property to make them the same."
            )
        return pyspark_catalog or sql_connector_catalog

    def set_current_catalog(self, catalog_name: str) -> None:
        def _set_spark_session_current_catalog(spark: PySparkSession) -> None:
            from py4j.protocol import Py4JError
            from pyspark.errors.exceptions.connect import SparkConnectGrpcException

            try:
                # Note: Spark 3.4+ Only API
                spark.catalog.setCurrentCatalog(catalog_name)
            except (Py4JError, SparkConnectGrpcException):
                pass

        # Since Databricks splits commands across the Dataframe API and the SQL Connector
        # (depending if databricks-connect is installed and a Dataframe is used) we need to ensure both
        # are set to the same catalog since they maintain their default catalog separately
        self.execute(exp.Use(this=exp.to_identifier(catalog_name), kind="CATALOG"))
        if self.is_spark_session_connection:
            _set_spark_session_current_catalog(self.connection.spark)

        if self._spark_engine_adapter:
            _set_spark_session_current_catalog(self._spark_engine_adapter.spark)

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and catalog.
        """
        schema = to_schema(schema_name)
        catalog_name = schema.catalog or self.get_current_catalog()
        query = (
            exp.select(
                exp.column("table_name").as_("name"),
                exp.column("table_schema").as_("schema"),
                exp.column("table_catalog").as_("catalog"),
                exp.case(exp.column("table_type"))
                .when(exp.Literal.string("VIEW"), exp.Literal.string("view"))
                .when(
                    exp.Literal.string("MATERIALIZED_VIEW"), exp.Literal.string("materialized_view")
                )
                .else_(exp.Literal.string("table"))
                .as_("type"),
            )
            .from_(
                # always query `system` information_schema
                exp.table_("tables", "information_schema", "system")
            )
            .where(exp.column("table_catalog").eq(catalog_name))
            .where(exp.column("table_schema").eq(schema.db))
        )

        if object_names:
            query = query.where(exp.column("table_name").isin(*object_names))

        df = self.fetchdf(query)
        return [
            DataObject(
                catalog=row.catalog,  # type: ignore
                schema=row.schema,  # type: ignore
                name=row.name,  # type: ignore
                type=DataObjectType.from_str(row.type),  # type: ignore
            )
            for row in df.itertuples()
        ]

    def clone_table(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        replace: bool = False,
        exists: bool = True,
        clone_kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        **kwargs: t.Any,
    ) -> None:
        clone_kwargs = clone_kwargs or {}
        clone_kwargs["shallow"] = True
        super().clone_table(
            target_table_name,
            source_table_name,
            replace=replace,
            clone_kwargs=clone_kwargs,
            **kwargs,
        )

    def wap_supported(self, table_name: TableName) -> bool:
        return False

    def close(self) -> t.Any:
        """Closes all open connections and releases all allocated resources."""
        super().close()
        if self._spark_engine_adapter:
            self._spark_engine_adapter.close()

    @property
    def default_catalog(self) -> t.Optional[str]:
        try:
            return super().default_catalog
        except MissingDefaultCatalogError as e:
            raise MissingDefaultCatalogError(
                "Could not determine default catalog. Define the connection property `catalog` since it can't be inferred from your connection. See SQLMesh Databricks documentation for details"
            ) from e

    def _build_table_properties_exp(
        self,
        catalog_name: t.Optional[str] = None,
        table_format: t.Optional[str] = None,
        storage_format: t.Optional[str] = None,
        partitioned_by: t.Optional[t.List[exp.Expression]] = None,
        partition_interval_unit: t.Optional[IntervalUnit] = None,
        clustered_by: t.Optional[t.List[exp.Expression]] = None,
        table_properties: t.Optional[t.Dict[str, exp.Expression]] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        table_kind: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        properties = super()._build_table_properties_exp(
            catalog_name=catalog_name,
            table_format=table_format,
            storage_format=storage_format,
            partitioned_by=partitioned_by,
            partition_interval_unit=partition_interval_unit,
            clustered_by=clustered_by,
            table_properties=table_properties,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            table_kind=table_kind,
        )
        if clustered_by:
            # Databricks expects wrapped CLUSTER BY expressions
            clustered_by_exp = exp.Cluster(
                expressions=[exp.Tuple(expressions=[c.copy() for c in clustered_by])]
            )
            expressions = properties.expressions if properties else []
            expressions.append(clustered_by_exp)
            properties = exp.Properties(expressions=expressions)
        return properties

    def _build_column_defs(
        self,
        target_columns_to_types: t.Dict[str, exp.DataType],
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        is_view: bool = False,
        materialized: bool = False,
    ) -> t.List[exp.ColumnDef]:
        # Databricks requires column types to be specified when adding column comments
        # in CREATE MATERIALIZED VIEW statements. Override is_view to False to force
        # column types to be included when comments are present.
        if is_view and materialized and column_descriptions:
            is_view = False

        return super()._build_column_defs(
            target_columns_to_types, column_descriptions, is_view, materialized
        )
