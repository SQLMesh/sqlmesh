from __future__ import annotations

import logging
import typing as t
from collections import defaultdict

from sqlglot import exp, parse_one
from sqlglot.transforms import remove_precision_parameterized_types

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.base import _get_data_object_cache_key
from sqlmesh.core.engine_adapter.mixins import (
    ClusteredByMixin,
    GrantsFromInfoSchemaMixin,
    RowDiffMixin,
    TableAlterClusterByOperation,
)
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    DataObject,
    DataObjectType,
    SourceQuery,
    set_catalog,
    InsertOverwriteStrategy,
)
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.schema_diff import TableAlterOperation, NestedSupport
from sqlmesh.utils import optional_import, get_source_columns_to_types
from sqlmesh.utils.date import to_datetime
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.pandas import columns_to_types_from_dtypes

if t.TYPE_CHECKING:
    import pandas as pd
    from google.api_core.retry import Retry
    from google.cloud import bigquery
    from google.cloud.bigquery import StandardSqlDataType
    from google.cloud.bigquery.client import Client as BigQueryClient
    from google.cloud.bigquery.job import QueryJob
    from google.cloud.bigquery.job.base import _AsyncJob as BigQueryQueryResult
    from google.cloud.bigquery.table import Table as BigQueryTable

    from sqlmesh.core._typing import SchemaName, SessionProperties, TableName
    from sqlmesh.core.engine_adapter._typing import BigframeSession, DCL, DF, GrantsConfig, Query
    from sqlmesh.core.engine_adapter.base import QueryOrDF


logger = logging.getLogger(__name__)

bigframes = optional_import("bigframes")
bigframes_pd = optional_import("bigframes.pandas")


NestedField = t.Tuple[str, str, t.List[str]]
NestedFieldsDict = t.Dict[str, t.List[NestedField]]


@set_catalog()
class BigQueryEngineAdapter(ClusteredByMixin, RowDiffMixin, GrantsFromInfoSchemaMixin):
    """
    BigQuery Engine Adapter using the `google-cloud-bigquery` library's DB API.
    """

    DIALECT = "bigquery"
    DEFAULT_BATCH_SIZE = 1000
    SUPPORTS_TRANSACTIONS = False
    SUPPORTS_MATERIALIZED_VIEWS = True
    SUPPORTS_CLONING = True
    SUPPORTS_GRANTS = True
    CURRENT_USER_OR_ROLE_EXPRESSION: exp.Expr = exp.func("session_user")
    SUPPORTS_MULTIPLE_GRANT_PRINCIPALS = True
    USE_CATALOG_IN_GRANTS = True
    GRANT_INFORMATION_SCHEMA_TABLE_NAME = "OBJECT_PRIVILEGES"
    MAX_TABLE_COMMENT_LENGTH = 1024
    MAX_COLUMN_COMMENT_LENGTH = 1024
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    SUPPORTED_DROP_CASCADE_OBJECT_KINDS = ["SCHEMA"]
    INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.MERGE

    SCHEMA_DIFFER_KWARGS = {
        "compatible_types": {
            exp.DataType.build("INT64", dialect=DIALECT): {
                exp.DataType.build("NUMERIC", dialect=DIALECT),
                exp.DataType.build("FLOAT64", dialect=DIALECT),
                exp.DataType.build("BIGNUMERIC", dialect=DIALECT),
            },
            exp.DataType.build("NUMERIC", dialect=DIALECT): {
                exp.DataType.build("FLOAT64", dialect=DIALECT),
                exp.DataType.build("BIGNUMERIC", dialect=DIALECT),
            },
            exp.DataType.build("DATE", dialect=DIALECT): {
                exp.DataType.build("DATETIME", dialect=DIALECT),
            },
        },
        "coerceable_types": {
            exp.DataType.build("FLOAT64", dialect=DIALECT): {
                exp.DataType.build("BIGNUMERIC", dialect=DIALECT),
            },
        },
        "support_coercing_compatible_types": True,
        "parameterized_type_defaults": {
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(38, 9), (0,)],
            exp.DataType.build("BIGDECIMAL", dialect=DIALECT).this: [(76.76, 38), (0,)],
        },
        "types_with_unlimited_length": {
            # parameterized `STRING(n)` can ALTER to unparameterized `STRING`
            exp.DataType.build("STRING", dialect=DIALECT).this: {
                exp.DataType.build("STRING", dialect=DIALECT).this,
            },
            # parameterized `BYTES(n)` can ALTER to unparameterized `BYTES`
            exp.DataType.build("BYTES", dialect=DIALECT).this: {
                exp.DataType.build("BYTES", dialect=DIALECT).this,
            },
        },
        "nested_support": NestedSupport.ALL_BUT_DROP,
    }

    @property
    def client(self) -> BigQueryClient:
        return self.connection._client

    @property
    def bigframe(self) -> t.Optional[BigframeSession]:
        if bigframes:
            options = bigframes.BigQueryOptions(
                credentials=self.client._credentials,
                project=self.client.project,
                location=self.client.location,
            )
            return bigframes.connect(context=options)
        return None

    @property
    def _job_params(self) -> t.Dict[str, t.Any]:
        from sqlmesh.core.config.connection import BigQueryPriority

        params = {
            "use_legacy_sql": False,
            "priority": self._extra_config.get(
                "priority", BigQueryPriority.INTERACTIVE.bigquery_constant
            ),
        }
        if self._extra_config.get("maximum_bytes_billed") is not None:
            params["maximum_bytes_billed"] = self._extra_config.get("maximum_bytes_billed")
        if self._extra_config.get("reservation") is not None:
            params["reservation"] = self._extra_config.get("reservation")
        if self.correlation_id:
            # BigQuery label keys must be lowercase
            key = self.correlation_id.job_type.value.lower()
            params["labels"] = {key: self.correlation_id.job_id}
        return params

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.FULL_SUPPORT

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        import pandas as pd

        source_columns_to_types = get_source_columns_to_types(
            target_columns_to_types, source_columns
        )

        temp_bq_table = self.__get_temp_bq_table(
            self._get_temp_table(target_table or "pandas"), source_columns_to_types
        )
        temp_table = exp.table_(
            temp_bq_table.table_id,
            db=temp_bq_table.dataset_id,
            catalog=temp_bq_table.project,
        )

        def query_factory() -> Query:
            ordered_df = df[list(source_columns_to_types)]
            if bigframes_pd and isinstance(ordered_df, bigframes_pd.DataFrame):
                ordered_df.to_gbq(
                    f"{temp_bq_table.project}.{temp_bq_table.dataset_id}.{temp_bq_table.table_id}",
                    if_exists="replace",
                )
            elif not self.table_exists(temp_table):
                # Make mypy happy
                assert isinstance(ordered_df, pd.DataFrame)
                self._db_call(self.client.create_table, table=temp_bq_table, exists_ok=False)
                result = self.__load_pandas_to_table(
                    temp_bq_table, ordered_df, source_columns_to_types, replace=False
                )
                if result.errors:
                    raise SQLMeshError(result.errors)
            return exp.select(
                *self._casted_columns(target_columns_to_types, source_columns=source_columns)
            ).from_(temp_table)

        return [
            SourceQuery(
                query_factory=query_factory,
                cleanup_func=lambda: self.drop_table(temp_table),
            )
        ]

    def close(self) -> t.Any:
        # Cancel all pending query jobs across all threads
        all_query_jobs = self._connection_pool.get_all_attributes("query_job")
        for query_job in all_query_jobs:
            if query_job:
                try:
                    if not self._db_call(query_job.done):
                        self._db_call(query_job.cancel)
                        logger.debug(
                            "Cancelled BigQuery job: https://console.cloud.google.com/bigquery?project=%s&j=bq:%s:%s",
                            query_job.project,
                            query_job.location,
                            query_job.job_id,
                        )
                except Exception as ex:
                    logger.debug(
                        "Failed to cancel BigQuery job: https://console.cloud.google.com/bigquery?project=%s&j=bq:%s:%s. %s",
                        query_job.project,
                        query_job.location,
                        query_job.job_id,
                        str(ex),
                    )

        return super().close()

    def _begin_session(self, properties: SessionProperties) -> None:
        from google.cloud.bigquery import QueryJobConfig

        query_label_property = properties.get("query_label")
        parsed_query_label: list[tuple[str, str]] = []
        if isinstance(query_label_property, (exp.Array, exp.Paren, exp.Tuple)):
            label_tuples = (
                [query_label_property.unnest()]
                if isinstance(query_label_property, exp.Paren)
                else query_label_property.expressions
            )

            # query_label is a Paren, Array or Tuple of 2-tuples and validated at load time
            parsed_query_label.extend(
                (label_tuple.expressions[0].name, label_tuple.expressions[1].name)
                for label_tuple in label_tuples
            )
        elif query_label_property is not None:
            raise SQLMeshError(
                "Invalid value for `session_properties.query_label`. Must be an array or tuple."
            )

        if self.correlation_id:
            parsed_query_label.append(
                (self.correlation_id.job_type.value.lower(), self.correlation_id.job_id)
            )

        if parsed_query_label:
            query_label_str = ",".join([":".join(label) for label in parsed_query_label])
            query = f'SET @@query_label = "{query_label_str}";SELECT 1;'
        else:
            query = "SELECT 1;"

        job = self.client.query(
            query,
            job_config=QueryJobConfig(create_session=True),
        )
        session_info = job.session_info
        session_id = session_info.session_id if session_info else None
        self._session_id = session_id
        job.result()

    def _end_session(self) -> None:
        self._session_id = None

    def _is_session_active(self) -> bool:
        return self._session_id is not None

    def get_current_catalog(self) -> t.Optional[str]:
        """Returns the catalog name of the current connection."""
        return self.client.project

    def set_current_catalog(self, catalog: str) -> None:
        """Sets the catalog name of the current connection."""
        self.client.project = catalog

    def create_schema(
        self,
        schema_name: SchemaName,
        ignore_if_exists: bool = True,
        warn_on_error: bool = True,
        properties: t.List[exp.Expr] = [],
    ) -> None:
        """Create a schema from a name or qualified table name."""
        from google.api_core.exceptions import Conflict

        try:
            super().create_schema(
                schema_name,
                ignore_if_exists=ignore_if_exists,
                warn_on_error=False,
            )
        except Exception as e:
            is_already_exists_error = isinstance(e, Conflict) and "Already Exists:" in str(e)
            if is_already_exists_error and ignore_if_exists:
                return
            if not warn_on_error:
                raise
            logger.warning("Failed to create schema '%s': %s", schema_name, e)

    def get_bq_schema(self, table_name: TableName) -> t.List[bigquery.SchemaField]:
        table = exp.to_table(table_name)
        if len(table.parts) == 3 and "." in table.name:
            self.execute(exp.select("*").from_(table).limit(0))
            query_job = self._query_job
            assert query_job is not None
            return query_job._query_results.schema
        return self._get_table(table).schema

    def columns(
        self, table_name: TableName, include_pseudo_columns: bool = False
    ) -> t.Dict[str, exp.DataType]:
        """Fetches column names and types for the target table."""

        def dtype_to_sql(
            dtype: t.Optional[StandardSqlDataType], field: bigquery.SchemaField
        ) -> str:
            assert dtype
            assert field

            kind = dtype.type_kind
            assert kind

            # Not using the enum value to preserve compatibility with older versions
            # of the BigQuery library.
            if kind.name == "ARRAY":
                return f"ARRAY<{dtype_to_sql(dtype.array_element_type, field)}>"
            if kind.name == "STRUCT":
                struct_type = dtype.struct_type
                assert struct_type
                fields = ", ".join(
                    f"{struct_field.name} {dtype_to_sql(struct_field.type, nested_field)}"
                    for struct_field, nested_field in zip(struct_type.fields, field.fields)
                )
                return f"STRUCT<{fields}>"
            if kind.name == "TYPE_KIND_UNSPECIFIED":
                field_type = field.field_type

                if field_type == "RANGE":
                    # If the field is a RANGE then `range_element_type` should be set to
                    # one of `"DATE"`, `"DATETIME"` or `"TIMESTAMP"`.
                    return f"RANGE<{field.range_element_type.element_type}>"

                return field_type

            return kind.name

        def create_mapping_schema(
            schema: t.Sequence[bigquery.SchemaField],
        ) -> t.Dict[str, exp.DataType]:
            return {
                field.name: exp.DataType.build(
                    dtype_to_sql(field.to_standard_sql().type, field), dialect=self.dialect
                )
                for field in schema
            }

        table = exp.to_table(table_name)
        if len(table.parts) == 3 and "." in table.name:
            # The client's `get_table` method can't handle paths with >3 identifiers
            self.execute(exp.select("*").from_(table).limit(0))
            query_job = self._query_job
            assert query_job is not None

            query_results = query_job._query_results
            columns = create_mapping_schema(query_results.schema)
        else:
            bq_table = self._get_table(table)
            columns = create_mapping_schema(bq_table.schema)

            if include_pseudo_columns:
                if bq_table.time_partitioning and not bq_table.time_partitioning.field:
                    columns["_PARTITIONTIME"] = exp.DataType.build("TIMESTAMP", dialect="bigquery")
                    if bq_table.time_partitioning.type_ == "DAY":
                        columns["_PARTITIONDATE"] = exp.DataType.build("DATE")
                if bq_table.table_id.endswith("*"):
                    columns["_TABLE_SUFFIX"] = exp.DataType.build("STRING", dialect="bigquery")
                if (
                    bq_table.external_data_configuration is not None
                    and bq_table.external_data_configuration.source_format
                    in (
                        "CSV",
                        "NEWLINE_DELIMITED_JSON",
                        "AVRO",
                        "PARQUET",
                        "ORC",
                        "DATASTORE_BACKUP",
                    )
                ):
                    columns["_FILE_NAME"] = exp.DataType.build("STRING", dialect="bigquery")

        return columns

    def alter_table(
        self,
        alter_expressions: t.Union[t.List[exp.Alter], t.List[TableAlterOperation]],
    ) -> None:
        """
        Performs the alter statements to change the current table into the structure of the target table,
        and uses the API to add columns to structs, where SQL is not supported.
        """
        if not alter_expressions:
            return

        cluster_by_operations, alter_statements = [], []
        for e in alter_expressions:
            if isinstance(e, TableAlterClusterByOperation):
                cluster_by_operations.append(e)
            elif isinstance(e, TableAlterOperation):
                alter_statements.append(e.expression)
            else:
                alter_statements.append(e)

        for op in cluster_by_operations:
            self._update_clustering_key(op)

        nested_fields, non_nested_expressions = self._split_alter_expressions(alter_statements)

        if nested_fields:
            self._update_table_schema_nested_fields(nested_fields, alter_statements[0].this)

        if non_nested_expressions:
            super().alter_table(non_nested_expressions)

    def fetchone(
        self,
        query: t.Union[exp.Expr, str],
        ignore_unsupported_errors: bool = False,
        quote_identifiers: bool = False,
    ) -> t.Optional[t.Tuple]:
        """
        BigQuery's `fetchone` method doesn't call execute and therefore would not benefit from the execute
        configuration we have in place. Therefore this implementation calls execute instead.
        """
        self.execute(
            query,
            ignore_unsupported_errors=ignore_unsupported_errors,
            quote_identifiers=quote_identifiers,
        )
        try:
            return next(self._query_data)
        except StopIteration:
            return None

    def fetchall(
        self,
        query: t.Union[exp.Expr, str],
        ignore_unsupported_errors: bool = False,
        quote_identifiers: bool = False,
    ) -> t.List[t.Tuple]:
        """
        BigQuery's `fetchone` method doesn't call execute and therefore would not benefit from the execute
        configuration we have in place. Therefore this implementation calls execute instead.
        """
        self.execute(
            query,
            ignore_unsupported_errors=ignore_unsupported_errors,
            quote_identifiers=quote_identifiers,
        )
        return list(self._query_data)

    def _split_alter_expressions(
        self,
        alter_expressions: t.List[exp.Alter],
    ) -> t.Tuple[NestedFieldsDict, t.List[exp.Alter]]:
        """
        Returns a dictionary of the nested fields to add and a list of the non-nested alter expressions.
        """
        nested_fields_to_add: NestedFieldsDict = defaultdict(list)
        non_nested_expressions = []

        for alter_expression in alter_expressions:
            action = alter_expression.args["actions"][0]
            if (
                isinstance(action, exp.ColumnDef)
                and isinstance(action.this, exp.Dot)
                and isinstance(action.kind, exp.DataType)
            ):
                root_field, *leaf_fields = action.this.this.sql(dialect=self.dialect).split(".")
                new_field = action.this.expression.sql(dialect=self.dialect)
                data_type = action.kind.sql(dialect=self.dialect)
                nested_fields_to_add[root_field].append((new_field, data_type, leaf_fields))
            else:
                non_nested_expressions.append(alter_expression)

        return nested_fields_to_add, non_nested_expressions

    def _build_nested_fields(
        self,
        current_fields: t.List[bigquery.SchemaField],
        fields_to_add: t.List[NestedField],
    ) -> t.List[bigquery.SchemaField]:
        """
        Recursively builds and updates the schema fields with the new nested fields.
        """
        from google.cloud import bigquery

        new_fields = []
        root: t.List[t.Tuple[str, str]] = []
        leaves: NestedFieldsDict = defaultdict(list)
        for new_field, data_type, leaf_fields in fields_to_add:
            if leaf_fields:
                leaves[leaf_fields[0]].append((new_field, data_type, leaf_fields[1:]))
            else:
                root.append((new_field, data_type))

        for field in current_fields:
            # If the new fields are nested, we need to recursively build them
            if field.name in leaves:
                subfields = list(field.fields)
                subfields = self._build_nested_fields(subfields, leaves[field.name])
                new_fields.append(
                    bigquery.SchemaField(
                        field.name, "RECORD", mode=field.mode, fields=tuple(subfields)
                    )
                )
            else:
                new_fields.append(field)

        # Build and append the new root-level fields
        new_fields.extend(
            self.__get_bq_schemafield(
                new_field[0], exp.DataType.build(new_field[1], dialect=self.dialect)
            )
            for new_field in root
        )
        return new_fields

    def _update_table_schema_nested_fields(
        self, nested_fields_to_add: NestedFieldsDict, table_name: str
    ) -> None:
        """
        Updates a BigQuery table schema by adding the new nested fields provided.
        """
        from google.cloud import bigquery

        table = self._get_table(table_name)
        original_schema = table.schema
        new_schema = []
        for field in original_schema:
            if field.name in nested_fields_to_add:
                fields = self._build_nested_fields(
                    list(field.fields), nested_fields_to_add[field.name]
                )
                new_schema.append(
                    bigquery.SchemaField(
                        field.name,
                        "RECORD",
                        mode=field.mode,
                        fields=tuple(fields),
                    )
                )
            else:
                new_schema.append(field)

        if new_schema != original_schema:
            table.schema = new_schema
            self.client.update_table(table, ["schema"])

    def __load_pandas_to_table(
        self,
        table: bigquery.Table,
        df: pd.DataFrame,
        columns_to_types: t.Dict[str, exp.DataType],
        replace: bool = False,
    ) -> BigQueryQueryResult:
        """
        Loads a pandas dataframe into a table in BigQuery. Will do an overwrite if replace is True. Note that
        the replace will replace the entire table, not just the rows that are in the dataframe.
        """
        from google.cloud import bigquery

        job_config = bigquery.job.LoadJobConfig(schema=self.__get_bq_schema(columns_to_types))
        if replace:
            job_config.write_disposition = bigquery.WriteDisposition.WRITE_TRUNCATE
        logger.info(f"Loading dataframe to BigQuery. Table Path: {table.path}")
        # This client call does not support retry so we don't use the `_db_call` method.
        result = self.__retry(
            self.__db_load_table_from_dataframe,
        )(df=df, table=table, job_config=job_config)
        if result.errors:
            raise SQLMeshError(result.errors)
        return result

    def __db_load_table_from_dataframe(
        self, df: pd.DataFrame, table: bigquery.Table, job_config: bigquery.LoadJobConfig
    ) -> BigQueryQueryResult:
        job = self.client.load_table_from_dataframe(
            dataframe=df, destination=table, job_config=job_config
        )
        return self._db_call(job.result)

    def __get_bq_schemafield(self, name: str, tpe: exp.DataType) -> bigquery.SchemaField:
        from google.cloud import bigquery

        mode = "NULLABLE"
        if tpe.is_type(exp.DataType.Type.ARRAY):
            mode = "REPEATED"
            tpe = tpe.expressions[0]

        field_type = tpe.sql(dialect=self.dialect)
        fields = []
        if tpe.is_type(*exp.DataType.NESTED_TYPES):
            field_type = "RECORD"
            for inner_field in tpe.expressions:
                if isinstance(inner_field, exp.ColumnDef):
                    inner_name = inner_field.this.sql(dialect=self.dialect)
                    inner_type = inner_field.kind
                    if inner_type is None:
                        raise ValueError(
                            f"cannot convert unknown type to BQ schema field {inner_field}"
                        )
                    fields.append(self.__get_bq_schemafield(name=inner_name, tpe=inner_type))
                else:
                    raise ValueError(f"unexpected nested expression {inner_field}")

        return bigquery.SchemaField(
            name=name,
            field_type=field_type,
            mode=mode,
            fields=fields,
        )

    def __get_bq_schema(
        self, columns_to_types: t.Dict[str, exp.DataType]
    ) -> t.List[bigquery.SchemaField]:
        """
        Returns a bigquery schema object from a dictionary of column names to types.
        """

        precisionless_col_to_types = {
            col_name: remove_precision_parameterized_types(col_type)
            for col_name, col_type in columns_to_types.items()
        }
        return [
            self.__get_bq_schemafield(name=col_name, tpe=t.cast(exp.DataType, col_type))
            for col_name, col_type in precisionless_col_to_types.items()
        ]

    def __get_temp_bq_table(
        self, table: exp.Table, columns_to_type: t.Dict[str, exp.DataType]
    ) -> bigquery.Table:
        """
        Returns a bigquery table object that is temporary and will expire in 3 hours.
        """
        bq_table = self.__get_bq_table(table, columns_to_type)
        bq_table.expires = to_datetime("in 3 hours")
        return bq_table

    def __get_bq_table(
        self, table: TableName, columns_to_type: t.Dict[str, exp.DataType]
    ) -> bigquery.Table:
        """
        Returns a bigquery table object with a schema defines that matches the columns_to_type dictionary.
        """
        from google.cloud import bigquery

        table_ = exp.to_table(table).copy()

        if not table_.catalog:
            table_.set("catalog", exp.to_identifier(self.default_catalog))

        return bigquery.Table(
            table_ref=self._table_name(table_),
            schema=self.__get_bq_schema(columns_to_type),
        )

    @property
    def __retry(self) -> Retry:
        from google.api_core import retry

        return retry.Retry(
            predicate=_ErrorCounter(self._extra_config["job_retries"]).should_retry,
            deadline=self._extra_config.get("job_retry_deadline_seconds"),
            initial=1.0,
            maximum=3.0,
        )

    def insert_overwrite_by_partition(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        partitioned_by: t.List[exp.Expr],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> None:
        if len(partitioned_by) != 1:
            raise SQLMeshError(
                f"Bigquery only supports partitioning by one column, {len(partitioned_by)} were provided."
            )

        partition_exp = partitioned_by[0]
        partition_column = partition_exp.find(exp.Column)

        granularity = partition_exp.args.get("unit")
        if granularity:
            granularity = granularity.name.lower()

        if not partition_column:
            partition_sql = partition_exp.sql(dialect=self.dialect)
            raise SQLMeshError(
                f"The partition expression '{partition_sql}' doesn't contain a column."
            )
        with (
            self.session({}),
            self.temp_table(
                query_or_df,
                name=table_name,
                partitioned_by=partitioned_by,
                source_columns=source_columns,
            ) as temp_table_name,
        ):
            if target_columns_to_types is None or target_columns_to_types[
                partition_column.name
            ] == exp.DataType.build("unknown"):
                target_columns_to_types = self.columns(table_name)

            partition_type_sql = target_columns_to_types[partition_column.name].sql(
                dialect=self.dialect
            )

            select_array_agg_partitions = select_partitions_expr(
                temp_table_name.db,
                temp_table_name.name,
                partition_type_sql,
                granularity=granularity,
                agg_func="ARRAY_AGG",
                catalog=temp_table_name.catalog or self.default_catalog,
            )

            self.execute(
                f"DECLARE _sqlmesh_target_partitions_ ARRAY<{partition_type_sql}> DEFAULT ({select_array_agg_partitions});"
            )

            where = t.cast(exp.Condition, partition_exp).isin(unnest="_sqlmesh_target_partitions_")

            self._insert_overwrite_by_condition(
                table_name,
                [SourceQuery(query_factory=lambda: exp.select("*").from_(temp_table_name))],
                target_columns_to_types,
                where=where,
            )

    def table_exists(self, table_name: TableName) -> bool:
        table = exp.to_table(table_name)
        data_object_cache_key = _get_data_object_cache_key(table.catalog, table.db, table.name)
        if data_object_cache_key in self._data_object_cache:
            logger.debug("Table existence cache hit: %s", data_object_cache_key)
            return self._data_object_cache[data_object_cache_key] is not None

        try:
            from google.cloud.exceptions import NotFound
        except ModuleNotFoundError:
            from google.api_core.exceptions import NotFound

        try:
            self._get_table(table_name)
            return True
        except NotFound:
            return False

    def get_table_last_modified_ts(self, table_names: t.List[TableName]) -> t.List[int]:
        from sqlmesh.utils.date import to_timestamp

        datasets_to_tables: t.DefaultDict[str, t.List[str]] = defaultdict(list)
        for table_name in table_names:
            table = exp.to_table(table_name)
            datasets_to_tables[table.db].append(table.name)

        results = []

        for dataset, tables in datasets_to_tables.items():
            query = (
                f"SELECT TIMESTAMP_MILLIS(last_modified_time) FROM `{dataset}.__TABLES__` WHERE "
            )
            for i, table_name in enumerate(tables):
                query += f"TABLE_ID = '{table_name}'"
                if i < len(tables) - 1:
                    query += " OR "
            results.extend(self.fetchall(query))

        return [to_timestamp(row[0]) for row in results]

    def _get_table(self, table_name: TableName) -> BigQueryTable:
        """
        Returns a BigQueryTable object for the given table name.

        Raises: `google.cloud.exceptions.NotFound` if the table does not exist.
        """
        return self._db_call(self.client.get_table, table=self._table_name(table_name))

    def _table_name(self, table_name: TableName) -> str:
        # the api doesn't support backticks, so we can't call exp.table_name or sql
        return ".".join(part.name for part in exp.to_table(table_name).parts)

    def _fetch_native_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> DF:
        self.execute(query, quote_identifiers=quote_identifiers)
        query_job = self._query_job
        assert query_job is not None
        return query_job.to_dataframe()

    def _create_column_comments(
        self,
        table_name: TableName,
        column_comments: t.Dict[str, str],
        table_kind: str = "TABLE",
        materialized_view: bool = False,
    ) -> None:
        if not (table_kind == "VIEW" and materialized_view):
            table = self._get_table(table_name)

            # convert Table object to dict
            table_def = table.to_api_repr()

            # Set column descriptions, supporting nested fields (e.g. record.field.nested_field)
            for column, comment in column_comments.items():
                fields = table_def["schema"]["fields"]
                field_names = column.split(".")
                last_index = len(field_names) - 1

                # Traverse the fields with nested fields down to leaf level
                for idx, name in enumerate(field_names):
                    if field := next((field for field in fields if field["name"] == name), None):
                        if idx == last_index:
                            field["description"] = self._truncate_comment(
                                comment, self.MAX_COLUMN_COMMENT_LENGTH
                            )
                        else:
                            fields = field.get("fields") or []

            # An "etag" is BQ versioning metadata that changes when an object is updated/modified. `update_table`
            # compares the etags of the table object passed to it and the remote table, erroring if the etags
            # don't match. We set the local etag to None to avoid this check.
            table_def["etag"] = None

            # convert dict back to a Table object
            table = table.from_api_repr(table_def)

            # update table schema
            logger.info(f"Registering column comments for table {table_name}")
            self._db_call(self.client.update_table, table=table, fields=["schema"])

    def _build_description_property_exp(
        self,
        description: str,
        trunc_method: t.Callable,
    ) -> exp.Property:
        return exp.Property(
            this=exp.to_identifier("description", quoted=True),
            value=exp.Literal.string(trunc_method(description)),
        )

    def _build_partitioned_by_exp(
        self,
        partitioned_by: t.List[exp.Expr],
        *,
        partition_interval_unit: t.Optional[IntervalUnit] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.PartitionedByProperty]:
        if len(partitioned_by) > 1:
            raise SQLMeshError("BigQuery only supports partitioning by a single column")

        this = partitioned_by[0]
        if (
            isinstance(this, exp.Column)
            and partition_interval_unit is not None
            and not partition_interval_unit.is_minute
        ):
            column_type: t.Optional[exp.DataType] = (target_columns_to_types or {}).get(this.name)

            if column_type == exp.DataType.build(
                "date", dialect=self.dialect
            ) and partition_interval_unit in (
                IntervalUnit.MONTH,
                IntervalUnit.YEAR,
            ):
                trunc_func = "DATE_TRUNC"
            elif column_type == exp.DataType.build("timestamp", dialect=self.dialect):
                trunc_func = "TIMESTAMP_TRUNC"
            elif column_type == exp.DataType.build("datetime", dialect=self.dialect):
                trunc_func = "DATETIME_TRUNC"
            else:
                trunc_func = ""

            if trunc_func:
                this = exp.func(
                    trunc_func,
                    this,
                    exp.var(partition_interval_unit.value.upper()),
                    dialect=self.dialect,
                )

        return exp.PartitionedByProperty(this=this)

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

        if partitioned_by and (
            partitioned_by_prop := self._build_partitioned_by_exp(
                partitioned_by,
                partition_interval_unit=partition_interval_unit,
                target_columns_to_types=target_columns_to_types,
            )
        ):
            properties.append(partitioned_by_prop)

        if clustered_by and (clustered_by_exp := self._build_clustered_by_exp(clustered_by)):
            properties.append(clustered_by_exp)

        if table_description:
            properties.append(
                self._build_description_property_exp(
                    table_description, self._truncate_table_comment
                ),
            )

        properties.extend(self._table_or_view_properties_to_expressions(table_properties))

        if properties:
            return exp.Properties(expressions=properties)
        return None

    def _build_column_def(
        self,
        col_name: str,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        engine_supports_schema_comments: bool = False,
        col_type: t.Optional[exp.DATA_TYPE] = None,
        nested_names: t.List[str] = [],
    ) -> exp.ColumnDef:
        # Helper function to build column definitions with column descriptions
        def _build_struct_with_descriptions(
            col_type: exp.DataType,
            nested_names: t.List[str],
        ) -> exp.DataType:
            column_expressions = []
            for column_def in col_type.expressions:
                # This is expected to  be true, but this check is included as a
                # precautionary measure in case of an unexpected edge case
                if isinstance(column_def, exp.ColumnDef):
                    column = self._build_column_def(
                        col_name=column_def.name,
                        column_descriptions=column_descriptions,
                        engine_supports_schema_comments=engine_supports_schema_comments,
                        col_type=column_def.kind,
                        nested_names=nested_names,
                    )
                else:
                    column = column_def
                column_expressions.append(column)
            return exp.DataType(this=col_type.this, expressions=column_expressions, nested=True)

        # Recursively build column definitions for BigQuery's RECORDs (struct) and REPEATED RECORDs (array of struct)
        if isinstance(col_type, exp.DataType) and col_type.expressions:
            expressions = col_type.expressions
            if col_type.is_type(exp.DataType.Type.STRUCT):
                col_type = _build_struct_with_descriptions(col_type, nested_names + [col_name])
            elif col_type.is_type(exp.DataType.Type.ARRAY) and expressions[0].is_type(
                exp.DataType.Type.STRUCT
            ):
                col_type = exp.DataType(
                    this=exp.DataType.Type.ARRAY,
                    expressions=[
                        _build_struct_with_descriptions(
                            col_type.expressions[0], nested_names + [col_name]
                        )
                    ],
                    nested=True,
                )

        return exp.ColumnDef(
            this=exp.to_identifier(col_name),
            kind=col_type,
            constraints=(
                self._build_col_comment_exp(
                    ".".join(nested_names + [col_name]), column_descriptions
                )
                if engine_supports_schema_comments and self.comments_enabled and column_descriptions
                else None
            ),
        )

    def _build_col_comment_exp(
        self, col_name: str, column_descriptions: t.Dict[str, str]
    ) -> t.List[exp.ColumnConstraint]:
        comment = column_descriptions.get(col_name, None)
        if comment:
            return [
                exp.ColumnConstraint(
                    kind=exp.Properties(
                        expressions=[
                            self._build_description_property_exp(
                                comment, self._truncate_column_comment
                            ),
                        ]
                    )
                )
            ]
        return []

    def _build_view_properties_exp(
        self,
        view_properties: t.Optional[t.Dict[str, exp.Expr]] = None,
        table_description: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        """Creates a SQLGlot table properties expression for view"""
        properties: t.List[exp.Expr] = []

        if table_description:
            properties.append(
                self._build_description_property_exp(
                    table_description, self._truncate_table_comment
                ),
            )

        properties.extend(self._table_or_view_properties_to_expressions(view_properties))

        if properties:
            return exp.Properties(expressions=properties)
        return None

    def _build_create_comment_table_exp(
        self, table: exp.Table, table_comment: str, table_kind: str
    ) -> exp.Comment | str:
        table_sql = table.sql(dialect=self.dialect, identify=True)

        truncated_comment = self._truncate_table_comment(table_comment)
        comment_sql = exp.Literal.string(truncated_comment).sql(dialect=self.dialect)

        return f"ALTER {table_kind} {table_sql} SET OPTIONS(description = {comment_sql})"

    def _build_create_comment_column_exp(
        self, table: exp.Table, column_name: str, column_comment: str, table_kind: str = "TABLE"
    ) -> exp.Comment | str:
        table_sql = table.sql(dialect=self.dialect, identify=True)
        column_sql = exp.column(column_name).sql(dialect=self.dialect, identify=True)

        truncated_comment = self._truncate_column_comment(column_comment)
        comment_sql = exp.Literal.string(truncated_comment).sql(dialect=self.dialect)

        return f"ALTER {table_kind} {table_sql} ALTER COLUMN {column_sql} SET OPTIONS(description = {comment_sql})"

    def create_state_table(
        self,
        table_name: str,
        target_columns_to_types: t.Dict[str, exp.DataType],
        primary_key: t.Optional[t.Tuple[str, ...]] = None,
    ) -> None:
        self.create_table(
            table_name,
            target_columns_to_types,
        )

    def _db_call(self, func: t.Callable[..., t.Any], *args: t.Any, **kwargs: t.Any) -> t.Any:
        return func(
            retry=self.__retry,
            *args,
            **kwargs,
        )

    def _execute(
        self,
        sql: str,
        track_rows_processed: bool = False,
        **kwargs: t.Any,
    ) -> None:
        """Execute a sql query."""
        from google.cloud.bigquery import QueryJobConfig
        from google.cloud.bigquery.query import ConnectionProperty

        # BigQuery's Python DB API implementation does not support retries, so we have to implement them ourselves.
        # So we update the cursor's query job and query data with the results of the new query job. This makes sure
        # that other cursor based operations execute correctly.
        session_id = self._session_id
        connection_properties = (
            [
                ConnectionProperty(key="session_id", value=session_id),
            ]
            if session_id
            else []
        )

        # Create job config
        job_config = QueryJobConfig(**self._job_params, connection_properties=connection_properties)

        self._query_job = self._db_call(
            self.client.query,
            query=sql,
            job_config=job_config,
            timeout=self._extra_config.get("job_creation_timeout_seconds"),
        )
        query_job = self._query_job
        assert query_job is not None

        logger.debug(
            "BigQuery job created: https://console.cloud.google.com/bigquery?project=%s&j=bq:%s:%s",
            query_job.project,
            query_job.location,
            query_job.job_id,
        )

        results = self._db_call(
            query_job.result,
            timeout=self._extra_config.get("job_execution_timeout_seconds"),  # type: ignore
        )

        self._query_data = iter(results) if results.total_rows else iter([])
        query_results = query_job._query_results
        self.cursor._set_rowcount(query_results)
        self.cursor._set_description(query_results.schema)

        if (
            track_rows_processed
            and self._query_execution_tracker
            and self._query_execution_tracker.is_tracking()
        ):
            num_rows = None
            if query_job.statement_type == "CREATE_TABLE_AS_SELECT":
                # since table was just created, number rows in table == number rows processed
                query_table = self.client.get_table(query_job.destination)
                num_rows = query_table.num_rows
            elif query_job.statement_type in ["INSERT", "DELETE", "MERGE", "UPDATE"]:
                num_rows = query_job.num_dml_affected_rows

            self._query_execution_tracker.record_execution(
                sql, num_rows, query_job.total_bytes_processed
            )

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and optionally catalog.
        """

        # The BigQuery Client's list_tables method does not support filtering by table name, so we have to
        # resort to using SQL instead.
        schema = to_schema(schema_name)
        catalog = schema.catalog or self.default_catalog
        query = (
            exp.select(
                exp.column("table_catalog").as_("catalog"),
                exp.column("table_name").as_("name"),
                exp.column("table_schema").as_("schema_name"),
                exp.case()
                .when(exp.column("table_type").eq("BASE TABLE"), exp.Literal.string("TABLE"))
                .when(exp.column("table_type").eq("CLONE"), exp.Literal.string("TABLE"))
                .when(exp.column("table_type").eq("EXTERNAL"), exp.Literal.string("TABLE"))
                .when(exp.column("table_type").eq("SNAPSHOT"), exp.Literal.string("TABLE"))
                .when(exp.column("table_type").eq("VIEW"), exp.Literal.string("VIEW"))
                .when(
                    exp.column("table_type").eq("MATERIALIZED VIEW"),
                    exp.Literal.string("MATERIALIZED_VIEW"),
                )
                .else_(exp.column("table_type"))
                .as_("type"),
                exp.column("clustering_key", "ci").as_("clustering_key"),
            )
            .with_(
                "clustering_info",
                as_=exp.select(
                    exp.column("table_catalog"),
                    exp.column("table_schema"),
                    exp.column("table_name"),
                    parse_one(
                        "string_agg(column_name order by clustering_ordinal_position)",
                        dialect=self.dialect,
                    ).as_("clustering_key"),
                )
                .from_(
                    exp.to_table(
                        f"`{catalog}`.`{schema.db}`.INFORMATION_SCHEMA.COLUMNS",
                        dialect=self.dialect,
                    )
                )
                .where(exp.column("clustering_ordinal_position").is_(exp.not_(exp.null())))
                .group_by("1", "2", "3"),
            )
            .from_(
                exp.to_table(
                    f"`{catalog}`.`{schema.db}`.INFORMATION_SCHEMA.TABLES", dialect=self.dialect
                )
            )
            .join(
                "clustering_info",
                using=["table_catalog", "table_schema", "table_name"],
                join_type="left",
                join_alias="ci",
            )
        )
        if object_names:
            query = query.where(exp.column("table_name").isin(*object_names))

        try:
            df = self.fetchdf(query, quote_identifiers=True)
        except Exception as e:
            if "Not found" in str(e):
                return []
            raise

        if df.empty:
            return []
        return [
            DataObject(
                catalog=row.catalog,  # type: ignore
                schema=row.schema_name,  # type: ignore
                name=row.name,  # type: ignore
                type=DataObjectType.from_str(row.type),  # type: ignore
                clustering_key=f"({row.clustering_key})" if row.clustering_key else None,  # type: ignore
            )
            for row in df.itertuples()
        ]

    def _update_clustering_key(self, operation: TableAlterClusterByOperation) -> None:
        cluster_key_expressions = getattr(operation, "cluster_key_expressions", [])
        bq_table = self._get_table(operation.target_table)

        rendered_columns = [c.sql(dialect=self.dialect) for c in cluster_key_expressions]
        bq_table.clustering_fields = (
            rendered_columns or None
        )  # causes a drop of the key if cluster_by is empty or None

        self._db_call(self.client.update_table, table=bq_table, fields=["clustering_fields"])

        if cluster_key_expressions:
            # BigQuery only applies new clustering going forward, so this rewrites the columns to apply the new clustering to historical data
            # ref: https://cloud.google.com/bigquery/docs/creating-clustered-tables#modifying-cluster-spec
            self.execute(
                exp.update(
                    operation.target_table,
                    {c: c for c in cluster_key_expressions},
                    where=exp.true(),
                )
            )

    def _normalize_decimal_value(self, col: exp.Expr, precision: int) -> exp.Expr:
        return exp.func("FORMAT", exp.Literal.string(f"%.{precision}f"), col)

    def _normalize_nested_value(self, col: exp.Expr) -> exp.Expr:
        return exp.func("TO_JSON_STRING", col, dialect=self.dialect)

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
        if (
            not target_columns_to_types
            and bigframes
            and isinstance(query_or_df, bigframes.dataframe.DataFrame)
        ):
            # using dry_run=True attempts to prevent the DataFrame from being materialized just to read the column types from it
            dtypes = query_or_df.to_pandas(dry_run=True).columnDtypes
            target_columns_to_types = columns_to_types_from_dtypes(dtypes.items())
            return target_columns_to_types, list(source_columns or target_columns_to_types)

        return super()._columns_to_types(
            query_or_df, target_columns_to_types, source_columns=source_columns
        )

    def _native_df_to_pandas_df(
        self,
        query_or_df: QueryOrDF,
    ) -> t.Union[Query, pd.DataFrame]:
        if bigframes and isinstance(query_or_df, bigframes.dataframe.DataFrame):
            return query_or_df.to_pandas()

        return super()._native_df_to_pandas_df(query_or_df)

    @property
    def _query_data(self) -> t.Any:
        return self._connection_pool.get_attribute("query_data")

    @_query_data.setter
    def _query_data(self, value: t.Any) -> None:
        self._connection_pool.set_attribute("query_data", value)

    @property
    def _query_job(self) -> t.Optional[QueryJob]:
        return self._connection_pool.get_attribute("query_job")

    @_query_job.setter
    def _query_job(self, value: t.Any) -> None:
        self._connection_pool.set_attribute("query_job", value)

    @property
    def _session_id(self) -> t.Any:
        return self._connection_pool.get_attribute("session_id")

    @_session_id.setter
    def _session_id(self, value: t.Any) -> None:
        self._connection_pool.set_attribute("session_id", value)

    def _get_current_schema(self) -> str:
        raise NotImplementedError("BigQuery does not support current schema")

    def _get_bq_dataset_location(self, project: str, dataset: str) -> str:
        return self._db_call(self.client.get_dataset, dataset_ref=f"{project}.{dataset}").location

    def _get_grant_expression(self, table: exp.Table) -> exp.Expr:
        if not table.db:
            raise ValueError(
                f"Table {table.sql(dialect=self.dialect)} does not have a schema (dataset)"
            )
        project = table.catalog or self.get_current_catalog()
        if not project:
            raise ValueError(
                f"Table {table.sql(dialect=self.dialect)} does not have a catalog (project)"
            )

        dataset = table.db
        table_name = table.name
        location = self._get_bq_dataset_location(project, dataset)

        # https://cloud.google.com/bigquery/docs/information-schema-object-privileges
        # OBJECT_PRIVILEGES is a project-level INFORMATION_SCHEMA view with regional qualifier
        object_privileges_table = exp.to_table(
            f"`{project}`.`region-{location}`.INFORMATION_SCHEMA.{self.GRANT_INFORMATION_SCHEMA_TABLE_NAME}",
            dialect=self.dialect,
        )
        return (
            exp.select("privilege_type", "grantee")
            .from_(object_privileges_table)
            .where(
                exp.and_(
                    exp.column("object_schema").eq(exp.Literal.string(dataset)),
                    exp.column("object_name").eq(exp.Literal.string(table_name)),
                    # Filter out current_user
                    # BigQuery grantees format: "user:email" or "group:name"
                    exp.func("split", exp.column("grantee"), exp.Literal.string(":"))[
                        exp.func("OFFSET", exp.Literal.number("1"))
                    ].neq(self.CURRENT_USER_OR_ROLE_EXPRESSION),
                )
            )
        )

    @staticmethod
    def _grant_object_kind(table_type: DataObjectType) -> str:
        if table_type == DataObjectType.VIEW:
            return "VIEW"
        if table_type == DataObjectType.MATERIALIZED_VIEW:
            # We actually need to use "MATERIALIZED VIEW" here even though it's not listed
            # as a supported resource_type in the BigQuery DCL doc:
            # https://cloud.google.com/bigquery/docs/reference/standard-sql/data-control-language
            return "MATERIALIZED VIEW"
        return "TABLE"

    def _dcl_grants_config_expr(
        self,
        dcl_cmd: t.Type[DCL],
        table: exp.Table,
        grants_config: GrantsConfig,
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> t.List[exp.Expr]:
        expressions: t.List[exp.Expr] = []
        if not grants_config:
            return expressions

        # https://cloud.google.com/bigquery/docs/reference/standard-sql/data-control-language

        def normalize_principal(p: str) -> str:
            if ":" not in p:
                raise ValueError(f"Principal '{p}' missing a prefix label")

            # allUsers and allAuthenticatedUsers special groups that are cas-sensitive and must start with "specialGroup:"
            if p.endswith("allUsers") or p.endswith("allAuthenticatedUsers"):
                if not p.startswith("specialGroup:"):
                    raise ValueError(
                        f"Special group principal '{p}' must start with 'specialGroup:' prefix label"
                    )
                return p

            label, principal = p.split(":", 1)
            # always lowercase principals
            return f"{label}:{principal.lower()}"

        object_kind = self._grant_object_kind(table_type)
        for privilege, principals in grants_config.items():
            if not principals:
                continue

            noramlized_principals = [exp.Literal.string(normalize_principal(p)) for p in principals]
            args: t.Dict[str, t.Any] = {
                "privileges": [exp.GrantPrivilege(this=exp.to_identifier(privilege, quoted=True))],
                "securable": table.copy(),
                "principals": noramlized_principals,
            }

            if object_kind:
                args["kind"] = exp.Var(this=object_kind)

            expressions.append(dcl_cmd(**args))  # type: ignore[arg-type]

        return expressions


class _ErrorCounter:
    """
    A class that counts errors and determines whether or not to retry based on the number of errors and the error
    type.

    Reference implementation: https://github.com/dbt-labs/dbt-bigquery/blob/8339a034929b12e027f0a143abf46582f3f6ffbc/dbt/adapters/bigquery/connections.py#L672

    TODO: Implement a retry configuration that works across all engines
    """

    def __init__(self, num_retries: int) -> None:
        self.num_retries = num_retries
        self.error_count = 0

    @property
    def retryable_errors(self) -> t.Tuple[t.Type[Exception], ...]:
        try:
            from google.cloud.exceptions import ServerError
        except ModuleNotFoundError:
            from google.api_core.exceptions import ServerError
        from requests.exceptions import ConnectionError

        return (ServerError, ConnectionError)

    def _is_retryable(self, error: BaseException) -> bool:
        from google.api_core.exceptions import Forbidden

        if isinstance(error, self.retryable_errors):
            return True
        if isinstance(error, Forbidden) and any(
            e["reason"] == "rateLimitExceeded" for e in error.errors
        ):
            return True
        return False

    def should_retry(self, error: BaseException) -> bool:
        if self.num_retries == 0:
            return False
        self.error_count += 1
        if self._is_retryable(error) and self.error_count <= self.num_retries:
            logger.info(f"Retry Num {self.error_count} of {self.num_retries}. Error: {repr(error)}")
            return True
        return False


def select_partitions_expr(
    schema: str,
    table_name: str,
    data_type: t.Union[str, exp.DataType],
    granularity: t.Optional[str] = None,
    agg_func: str = "MAX",
    catalog: t.Optional[str] = None,
) -> str:
    """Generates a SQL expression that aggregates partition values for a table.

    Args:
        schema: The schema (BigQuery dataset) of the table.
        table_name: The name of the table.
        data_type: The data type of the partition column.
        granularity: The granularity of the partition. Supported values are: 'day', 'month', 'year' and 'hour'.
        agg_func: The aggregation function to use.
        catalog: The catalog (BigQuery project ID) of the table.

    Returns:
        A SELECT statement that aggregates partition values for a table.
    """
    partitions_table_name = f"`{schema}`.INFORMATION_SCHEMA.PARTITIONS"
    if catalog:
        partitions_table_name = f"`{catalog}`.{partitions_table_name}"

    if isinstance(data_type, exp.DataType):
        data_type = data_type.sql(dialect="bigquery")
    data_type = data_type.upper()

    parse_fun = f"PARSE_{data_type}" if data_type in ("DATE", "DATETIME", "TIMESTAMP") else None
    if parse_fun:
        granularity = granularity or "day"
        parse_format = GRANULARITY_TO_PARTITION_FORMAT[granularity.lower()]
        partition_expr = exp.func(
            parse_fun,
            exp.Literal.string(parse_format),
            exp.column("partition_id"),
            dialect="bigquery",
        )
    else:
        partition_expr = exp.cast(exp.column("partition_id"), "INT64", dialect="bigquery")

    return (
        exp.select(exp.func(agg_func, partition_expr))
        .from_(partitions_table_name, dialect="bigquery")
        .where(
            f"table_name = '{table_name}' AND partition_id IS NOT NULL AND partition_id != '__NULL__'",
            copy=False,
        )
        .sql(dialect="bigquery")
    )


GRANULARITY_TO_PARTITION_FORMAT = {
    "day": "%Y%m%d",
    "month": "%Y%m",
    "year": "%Y",
    "hour": "%Y%m%d%H",
}
