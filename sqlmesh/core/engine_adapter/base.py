"""
# EngineAdapter

Engine adapters are how SQLMesh connects and interacts with various data stores. They allow SQLMesh to
generalize its functionality to different engines that have Python Database API 2.0-compliant
connections. Rather than executing queries directly against your data stores, SQLMesh components such as
the SnapshotEvaluator delegate them to engine adapters so these components can be engine-agnostic.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import sys
import typing as t
from functools import cached_property, partial

from sqlglot import Dialect, exp
from sqlglot.errors import ErrorLevel
from sqlglot.helper import ensure_list, seq_get
from sqlglot.optimizer.qualify_columns import quote_identifiers

from sqlmesh.core.dialect import (
    add_table,
    schema_,
    select_from_values_for_batch_range,
    to_schema,
)
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
    EngineRunMode,
    InsertOverwriteStrategy,
    SourceQuery,
    set_catalog,
)
from sqlmesh.core.model.kind import TimeColumn
from sqlmesh.core.schema_diff import SchemaDiffer, TableAlterOperation
from sqlmesh.core.snapshot.execution_tracker import QueryExecutionTracker
from sqlmesh.utils import (
    CorrelationId,
    columns_to_types_all_known,
    random_id,
    get_source_columns_to_types,
)
from sqlmesh.utils.connection_pool import ConnectionPool, create_connection_pool
from sqlmesh.utils.date import TimeLike, make_inclusive, to_time_column
from sqlmesh.utils.errors import (
    MissingDefaultCatalogError,
    SQLMeshError,
    UnsupportedCatalogOperationError,
)
from sqlmesh.utils.pandas import columns_to_types_from_df

if t.TYPE_CHECKING:
    import pandas as pd

    from sqlmesh.core._typing import SchemaName, SessionProperties, TableName
    from sqlmesh.core.engine_adapter._typing import (
        DF,
        BigframeSession,
        GrantsConfig,
        PySparkDataFrame,
        PySparkSession,
        Query,
        QueryOrDF,
        SnowparkSession,
    )
    from sqlmesh.core.node import IntervalUnit

logger = logging.getLogger(__name__)

MERGE_TARGET_ALIAS = "__MERGE_TARGET__"
MERGE_SOURCE_ALIAS = "__MERGE_SOURCE__"

KEY_FOR_CREATABLE_TYPE = "CREATABLE_TYPE"


@set_catalog()
class EngineAdapter:
    """Base class wrapping a Database API compliant connection.

    The EngineAdapter is an easily-subclassable interface that interacts
    with the underlying engine and data store.

    Args:
        connection_factory_or_pool: a callable which produces a new Database API-compliant
            connection on every call.
        dialect: The dialect with which this adapter is associated.
        multithreaded: Indicates whether this adapter will be used by more than one thread.
    """

    DIALECT = ""
    DEFAULT_BATCH_SIZE = 10000
    DATA_OBJECT_FILTER_BATCH_SIZE = 4000
    SUPPORTS_TRANSACTIONS = True
    SUPPORTS_INDEXES = False
    COMMENT_CREATION_TABLE = CommentCreationTable.IN_SCHEMA_DEF_CTAS
    COMMENT_CREATION_VIEW = CommentCreationView.IN_SCHEMA_DEF_AND_COMMANDS
    MAX_TABLE_COMMENT_LENGTH: t.Optional[int] = None
    MAX_COLUMN_COMMENT_LENGTH: t.Optional[int] = None
    INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.DELETE_INSERT
    SUPPORTS_MATERIALIZED_VIEWS = False
    SUPPORTS_MATERIALIZED_VIEW_SCHEMA = False
    SUPPORTS_VIEW_SCHEMA = True
    SUPPORTS_CLONING = False
    SUPPORTS_MANAGED_MODELS = False
    SUPPORTS_CREATE_DROP_CATALOG = False
    SUPPORTED_DROP_CASCADE_OBJECT_KINDS: t.List[str] = []
    SCHEMA_DIFFER_KWARGS: t.Dict[str, t.Any] = {}
    SUPPORTS_TUPLE_IN = True
    HAS_VIEW_BINDING = False
    SUPPORTS_REPLACE_TABLE = True
    SUPPORTS_GRANTS = False
    DEFAULT_CATALOG_TYPE = DIALECT
    QUOTE_IDENTIFIERS_IN_VIEWS = True
    MAX_IDENTIFIER_LENGTH: t.Optional[int] = None
    ATTACH_CORRELATION_ID = True
    SUPPORTS_QUERY_EXECUTION_TRACKING = False
    SUPPORTS_METADATA_TABLE_LAST_MODIFIED_TS = False

    def __init__(
        self,
        connection_factory_or_pool: t.Union[t.Callable[[], t.Any], ConnectionPool],
        dialect: str = "",
        sql_gen_kwargs: t.Optional[t.Dict[str, Dialect | bool | str]] = None,
        multithreaded: bool = False,
        cursor_init: t.Optional[t.Callable[[t.Any], None]] = None,
        default_catalog: t.Optional[str] = None,
        execute_log_level: int = logging.DEBUG,
        register_comments: bool = True,
        pre_ping: bool = False,
        pretty_sql: bool = False,
        shared_connection: bool = False,
        correlation_id: t.Optional[CorrelationId] = None,
        schema_differ_overrides: t.Optional[t.Dict[str, t.Any]] = None,
        query_execution_tracker: t.Optional[QueryExecutionTracker] = None,
        **kwargs: t.Any,
    ):
        self.dialect = dialect.lower() or self.DIALECT
        self._connection_pool = (
            connection_factory_or_pool
            if isinstance(connection_factory_or_pool, ConnectionPool)
            else create_connection_pool(
                connection_factory_or_pool,
                multithreaded,
                shared_connection=shared_connection,
                cursor_init=cursor_init,
            )
        )
        self._sql_gen_kwargs = sql_gen_kwargs or {}
        self._default_catalog = default_catalog
        self._execute_log_level = execute_log_level
        self._extra_config = kwargs
        self._register_comments = register_comments
        self._pre_ping = pre_ping
        self._pretty_sql = pretty_sql
        self._multithreaded = multithreaded
        self.correlation_id = correlation_id
        self._schema_differ_overrides = schema_differ_overrides
        self._query_execution_tracker = query_execution_tracker
        self._data_object_cache: t.Dict[str, t.Optional[DataObject]] = {}

    def with_settings(self, **kwargs: t.Any) -> EngineAdapter:
        extra_kwargs = {
            "null_connection": True,
            "execute_log_level": kwargs.pop("execute_log_level", self._execute_log_level),
            "correlation_id": kwargs.pop("correlation_id", self.correlation_id),
            "query_execution_tracker": kwargs.pop(
                "query_execution_tracker", self._query_execution_tracker
            ),
            **self._extra_config,
            **kwargs,
        }

        adapter = self.__class__(
            self._connection_pool,
            dialect=self.dialect,
            sql_gen_kwargs=self._sql_gen_kwargs,
            default_catalog=self._default_catalog,
            register_comments=self._register_comments,
            multithreaded=self._multithreaded,
            pretty_sql=self._pretty_sql,
            **extra_kwargs,
        )

        return adapter

    @property
    def cursor(self) -> t.Any:
        return self._connection_pool.get_cursor()

    @property
    def connection(self) -> t.Any:
        return self._connection_pool.get()

    @property
    def spark(self) -> t.Optional[PySparkSession]:
        return None

    @property
    def snowpark(self) -> t.Optional[SnowparkSession]:
        return None

    @property
    def bigframe(self) -> t.Optional[BigframeSession]:
        return None

    @property
    def comments_enabled(self) -> bool:
        return self._register_comments and self.COMMENT_CREATION_TABLE.is_supported

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.UNSUPPORTED

    @cached_property
    def schema_differ(self) -> SchemaDiffer:
        return SchemaDiffer(
            **{
                **self.SCHEMA_DIFFER_KWARGS,
                **(self._schema_differ_overrides or {}),
            }
        )

    @property
    def _catalog_type_overrides(self) -> t.Dict[str, str]:
        return self._extra_config.get("catalog_type_overrides") or {}

    @classmethod
    def _casted_columns(
        cls,
        target_columns_to_types: t.Dict[str, exp.DataType],
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[exp.Expr]:
        source_columns_lookup = set(source_columns or target_columns_to_types)
        return [
            exp.alias_(
                exp.cast(
                    exp.column(column, quoted=True)
                    if column in source_columns_lookup
                    else exp.Null(),
                    to=kind,
                ),
                column,
                copy=False,
                quoted=True,
            )
            for column, kind in target_columns_to_types.items()
        ]

    @property
    def default_catalog(self) -> t.Optional[str]:
        if self.catalog_support.is_unsupported:
            return None
        default_catalog = self._default_catalog or self.get_current_catalog()
        if not default_catalog:
            raise MissingDefaultCatalogError(
                "Could not determine a default catalog despite it being supported."
            )
        return default_catalog

    @property
    def engine_run_mode(self) -> EngineRunMode:
        return EngineRunMode.SINGLE_MODE_ENGINE

    def _get_source_queries(
        self,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]],
        target_table: TableName,
        *,
        batch_size: t.Optional[int] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        import pandas as pd

        batch_size = self.DEFAULT_BATCH_SIZE if batch_size is None else batch_size
        if isinstance(query_or_df, exp.Query):
            query_factory = lambda: query_or_df
            if source_columns:
                source_columns_lookup = set(source_columns)
                if not target_columns_to_types:
                    raise SQLMeshError("columns_to_types must be set if source_columns is set")
                if not set(target_columns_to_types).issubset(source_columns_lookup):
                    select_columns = [
                        exp.column(c, quoted=True)
                        if c in source_columns_lookup
                        else exp.cast(exp.Null(), target_columns_to_types[c], copy=False).as_(
                            c, copy=False, quoted=True
                        )
                        for c in target_columns_to_types
                    ]
                    query_factory = (
                        lambda: exp.Select()
                        .select(*select_columns)
                        .from_(query_or_df.subquery("select_source_columns"))
                    )
            return [SourceQuery(query_factory=query_factory)]  # type: ignore

        if not target_columns_to_types:
            raise SQLMeshError(
                "It is expected that if a DataFrame is passed in then columns_to_types is set"
            )

        if isinstance(query_or_df, pd.DataFrame) and query_or_df.empty:
            raise SQLMeshError(
                "Cannot construct source query from an empty DataFrame. This error is commonly "
                "related to Python models that produce no data. For such models, consider yielding "
                "from an empty generator if the resulting set is empty, i.e. use `yield from ()`."
            )

        return self._df_to_source_queries(
            query_or_df,
            target_columns_to_types,
            batch_size,
            target_table=target_table,
            source_columns=source_columns,
        )

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        import pandas as pd

        assert isinstance(df, pd.DataFrame)
        num_rows = len(df.index)
        batch_size = sys.maxsize if batch_size == 0 else batch_size

        # we need to ensure that the order of the columns in columns_to_types columns matches the order of the values
        # they can differ if a user specifies columns() on a python model in a different order than what's in the DataFrame's emitted by that model
        df = df[list(source_columns or target_columns_to_types)]
        values = list(df.itertuples(index=False, name=None))

        return [
            SourceQuery(
                query_factory=partial(
                    self._values_to_sql,
                    values=values,  # type: ignore
                    target_columns_to_types=target_columns_to_types,
                    batch_start=i,
                    batch_end=min(i + batch_size, num_rows),
                    source_columns=source_columns,
                ),
            )
            for i in range(0, num_rows, batch_size)
        ]

    def _get_source_queries_and_columns_to_types(
        self,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]],
        target_table: TableName,
        *,
        batch_size: t.Optional[int] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.Tuple[t.List[SourceQuery], t.Optional[t.Dict[str, exp.DataType]]]:
        target_columns_to_types, source_columns = self._columns_to_types(
            query_or_df, target_columns_to_types, source_columns
        )
        source_queries = self._get_source_queries(
            query_or_df,
            target_columns_to_types,
            target_table=target_table,
            batch_size=batch_size,
            source_columns=source_columns,
        )
        return source_queries, target_columns_to_types

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
        import pandas as pd

        if not target_columns_to_types and isinstance(query_or_df, pd.DataFrame):
            target_columns_to_types = columns_to_types_from_df(t.cast(pd.DataFrame, query_or_df))
        if not source_columns and target_columns_to_types:
            source_columns = list(target_columns_to_types)
        # source columns should only contain columns that are defined in the target. If there are extras then
        # that means they are intended to be ignored and will be excluded
        source_columns = (
            [x for x in source_columns if x in target_columns_to_types]
            if source_columns and target_columns_to_types
            else None
        )
        return target_columns_to_types, source_columns

    def recycle(self) -> None:
        """Closes all open connections and releases all allocated resources associated with any thread
        except the calling one."""
        self._connection_pool.close_all(exclude_calling_thread=True)

    def close(self) -> t.Any:
        """Closes all open connections and releases all allocated resources."""
        self._connection_pool.close_all()

    def get_current_catalog(self) -> t.Optional[str]:
        """Returns the catalog name of the current connection."""
        raise NotImplementedError()

    def set_current_catalog(self, catalog: str) -> None:
        """Sets the catalog name of the current connection."""
        raise NotImplementedError()

    def get_catalog_type(self, catalog: t.Optional[str]) -> str:
        """Intended to be overridden for data virtualization systems like Trino that,
        depending on the target catalog, require slightly different properties to be set when creating / updating tables
        """
        if self.catalog_support.is_unsupported:
            raise UnsupportedCatalogOperationError(
                f"{self.dialect} does not support catalogs and a catalog was provided: {catalog}"
            )
        return (
            self._catalog_type_overrides.get(catalog, self.DEFAULT_CATALOG_TYPE)
            if catalog
            else self.DEFAULT_CATALOG_TYPE
        )

    def get_catalog_type_from_table(self, table: TableName) -> str:
        """Get the catalog type from a table name if it has a catalog specified, otherwise return the current catalog type"""
        catalog = exp.to_table(table).catalog or self.get_current_catalog()
        return self.get_catalog_type(catalog)

    @property
    def current_catalog_type(self) -> str:
        # `get_catalog_type_from_table` should be used over this property. Reason is that the table that is the target
        # of the operation is what matters and not the catalog type of the connection.
        # This still remains for legacy reasons and should be refactored out.
        return self.get_catalog_type(self.get_current_catalog())

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
        """Replaces an existing table with a query.

        For partition based engines (hive, spark), insert override is used. For other systems, create or replace is used.

        Args:
            table_name: The name of the table (eg. prod.table)
            query_or_df: The SQL query to run or a dataframe.
            target_columns_to_types: Only used if a dataframe is provided. A mapping between the column name and its data type.
                Expected to be ordered to match the order of values in the dataframe.
            kwargs: Optional create table properties.
        """
        target_table = exp.to_table(table_name)

        target_data_object = self.get_data_object(target_table)
        table_exists = target_data_object is not None
        if self.drop_data_object_on_type_mismatch(target_data_object, DataObjectType.TABLE):
            table_exists = False

        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            target_columns_to_types,
            target_table=target_table,
            source_columns=source_columns,
        )
        if not target_columns_to_types and table_exists:
            target_columns_to_types = self.columns(target_table)
        query = source_queries[0].query_factory()
        self_referencing = any(
            quote_identifiers(table) == quote_identifiers(target_table)
            for table in query.find_all(exp.Table)
        )
        # If a query references itself then it must have a table created regardless of approach used.
        if self_referencing:
            if not target_columns_to_types:
                raise SQLMeshError(
                    f"Cannot create a self-referencing table {target_table.sql(dialect=self.dialect)} without knowing the column types. "
                    "Try casting the columns to an expected type or defining the columns in the model metadata. "
                )
            self._create_table_from_columns(
                target_table,
                target_columns_to_types,
                exists=True,
                table_description=table_description,
                column_descriptions=column_descriptions,
                **kwargs,
            )
        # All engines support `CREATE TABLE AS` so we use that if the table doesn't already exist and we
        # use `CREATE OR REPLACE TABLE AS` if the engine supports it
        supports_replace_table = (
            self.SUPPORTS_REPLACE_TABLE
            if supports_replace_table_override is None
            else supports_replace_table_override
        )
        if supports_replace_table or not table_exists:
            return self._create_table_from_source_queries(
                target_table,
                source_queries,
                target_columns_to_types,
                replace=supports_replace_table,
                table_description=table_description,
                column_descriptions=column_descriptions,
                **kwargs,
            )
        if self_referencing:
            assert target_columns_to_types is not None
            with self.temp_table(
                self._select_columns(target_columns_to_types).from_(target_table),
                name=target_table,
                target_columns_to_types=target_columns_to_types,
                **kwargs,
            ) as temp_table:
                for source_query in source_queries:
                    source_query.add_transform(
                        lambda node: (  # type: ignore
                            temp_table  # type: ignore
                            if isinstance(node, exp.Table)
                            and quote_identifiers(node) == quote_identifiers(target_table)
                            else node
                        )
                    )
                return self._insert_overwrite_by_condition(
                    target_table,
                    source_queries,
                    target_columns_to_types,
                    **kwargs,
                )
        return self._insert_overwrite_by_condition(
            target_table,
            source_queries,
            target_columns_to_types,
            **kwargs,
        )

    def create_index(
        self,
        table_name: TableName,
        index_name: str,
        columns: t.Tuple[str, ...],
        exists: bool = True,
    ) -> None:
        """Creates a new index for the given table if supported

        Args:
            table_name: The name of the target table.
            index_name: The name of the index.
            columns: The list of columns that constitute the index.
            exists: Indicates whether to include the IF NOT EXISTS check.
        """
        if not self.SUPPORTS_INDEXES:
            return

        expression = exp.Create(
            this=exp.Index(
                this=exp.to_identifier(index_name),
                table=exp.to_table(table_name),
                params=exp.IndexParameters(columns=[exp.to_column(c) for c in columns]),
            ),
            kind="INDEX",
            exists=exists,
        )
        self.execute(expression)

    def _pop_creatable_type_from_properties(
        self,
        properties: t.Dict[str, exp.Expr],
    ) -> t.Optional[exp.Property]:
        """Pop out the creatable_type from the properties dictionary (if exists (return it/remove it) else return none).
        It also checks that none of the expressions are MATERIALIZE as that conflicts with the `materialize` parameter.
        """
        for key in list(properties.keys()):
            upper_key = key.upper()
            if upper_key == KEY_FOR_CREATABLE_TYPE:
                value = properties.pop(key).name
                parsed_properties = exp.maybe_parse(
                    value, into=exp.Properties, dialect=self.dialect
                )
                property, *others = parsed_properties.expressions
                if others:
                    # Multiple properties are unsupported today, can look into it in the future if needed
                    raise SQLMeshError(
                        f"Invalid creatable_type value with multiple properties: {value}"
                    )
                if isinstance(property, exp.MaterializedProperty):
                    raise SQLMeshError(
                        f"Cannot use {value} as a creatable_type as it conflicts with the `materialize` parameter."
                    )
                return property
        return None

    def create_table(
        self,
        table_name: TableName,
        target_columns_to_types: t.Dict[str, exp.DataType],
        primary_key: t.Optional[t.Tuple[str, ...]] = None,
        exists: bool = True,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        **kwargs: t.Any,
    ) -> None:
        """Create a table using a DDL statement

        Args:
            table_name: The name of the table to create. Can be fully qualified or just table name.
            target_columns_to_types: A mapping between the column name and its data type.
            primary_key: Determines the table primary key.
            exists: Indicates whether to include the IF NOT EXISTS check.
            table_description: Optional table description from MODEL DDL.
            column_descriptions: Optional column descriptions from model query.
            kwargs: Optional create table properties.
        """
        self._create_table_from_columns(
            table_name,
            target_columns_to_types,
            primary_key,
            exists,
            table_description,
            column_descriptions,
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
        """Create a managed table using a query.

        "Managed" means that once the table is created, the data is kept up to date by the underlying database engine and not SQLMesh.

        Args:
            table_name: The name of the table to create. Can be fully qualified or just table name.
            query: The SQL query for the engine to base the managed table on
            target_columns_to_types: A mapping between the column name and its data type.
            partitioned_by: The partition columns or engine specific expressions, only applicable in certain engines. (eg. (ds, hour))
            clustered_by: The cluster columns or engine specific expressions, only applicable in certain engines. (eg. (ds, hour))
            table_properties: Optional mapping of engine-specific properties to be set on the managed table
            table_description: Optional table description from MODEL DDL.
            column_descriptions: Optional column descriptions from model query.
            kwargs: Optional create table properties.
        """
        raise NotImplementedError(f"Engine does not support managed tables: {type(self)}")

    def ctas(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        exists: bool = True,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        """Create a table using a CTAS statement

        Args:
            table_name: The name of the table to create. Can be fully qualified or just table name.
            query_or_df: The SQL query to run or a dataframe for the CTAS.
            target_columns_to_types: A mapping between the column name and its data type. Required if using a DataFrame.
            exists: Indicates whether to include the IF NOT EXISTS check.
            table_description: Optional table description from MODEL DDL.
            column_descriptions: Optional column descriptions from model query.
            kwargs: Optional create table properties.
        """
        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            target_columns_to_types,
            target_table=table_name,
            source_columns=source_columns,
        )
        return self._create_table_from_source_queries(
            table_name,
            source_queries,
            target_columns_to_types,
            exists,
            table_description=table_description,
            column_descriptions=column_descriptions,
            **kwargs,
        )

    def create_state_table(
        self,
        table_name: str,
        target_columns_to_types: t.Dict[str, exp.DataType],
        primary_key: t.Optional[t.Tuple[str, ...]] = None,
    ) -> None:
        """Create a table to store SQLMesh internal state.

        Args:
            table_name: The name of the table to create. Can be fully qualified or just table name.
            target_columns_to_types: A mapping between the column name and its data type.
            primary_key: Determines the table primary key.
        """
        self.create_table(
            table_name,
            target_columns_to_types,
            primary_key=primary_key,
        )

    def _create_table_from_columns(
        self,
        table_name: TableName,
        target_columns_to_types: t.Dict[str, exp.DataType],
        primary_key: t.Optional[t.Tuple[str, ...]] = None,
        exists: bool = True,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        **kwargs: t.Any,
    ) -> None:
        """
        Create a table using a DDL statement.

        Args:
            table_name: The name of the table to create. Can be fully qualified or just table name.
            target_columns_to_types: Mapping between the column name and its data type.
            primary_key: Determines the table primary key.
            exists: Indicates whether to include the IF NOT EXISTS check.
            table_description: Optional table description from MODEL DDL.
            column_descriptions: Optional column descriptions from model query.
            kwargs: Optional create table properties.
        """
        table = exp.to_table(table_name)

        if not columns_to_types_all_known(target_columns_to_types):
            # It is ok if the columns types are not known if the table already exists and IF NOT EXISTS is set
            if exists and self.table_exists(table_name):
                return
            raise SQLMeshError(
                "Cannot create a table without knowing the column types. "
                "Try casting the columns to an expected type or defining the columns in the model metadata. "
                f"Columns to types: {target_columns_to_types}"
            )

        primary_key_expression = (
            [exp.PrimaryKey(expressions=[exp.to_column(k) for k in primary_key])]
            if primary_key and self.SUPPORTS_INDEXES
            else []
        )

        schema = self._build_schema_exp(
            table,
            target_columns_to_types,
            column_descriptions,
            primary_key_expression,
        )

        self._create_table(
            schema,
            None,
            exists=exists,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            **kwargs,
        )

        # Register comments with commands if the engine doesn't support comments in the schema or CREATE
        if (
            table_description
            and self.COMMENT_CREATION_TABLE.is_comment_command_only
            and self.comments_enabled
        ):
            self._create_table_comment(table_name, table_description)
        if (
            column_descriptions
            and self.COMMENT_CREATION_TABLE.is_comment_command_only
            and self.comments_enabled
        ):
            self._create_column_comments(table_name, column_descriptions)

    def _build_schema_exp(
        self,
        table: exp.Table,
        target_columns_to_types: t.Dict[str, exp.DataType],
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        expressions: t.Optional[t.List[exp.PrimaryKey]] = None,
        is_view: bool = False,
        materialized: bool = False,
    ) -> exp.Schema:
        """
        Build a schema expression for a table, columns, column comments, and additional schema properties.
        """
        expressions = expressions or []

        return exp.Schema(
            this=table,
            expressions=self._build_column_defs(
                target_columns_to_types=target_columns_to_types,
                column_descriptions=column_descriptions,
                is_view=is_view,
                materialized=materialized,
            )
            + expressions,
        )

    def _build_column_defs(
        self,
        target_columns_to_types: t.Dict[str, exp.DataType],
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        is_view: bool = False,
        materialized: bool = False,
    ) -> t.List[exp.ColumnDef]:
        engine_supports_schema_comments = (
            self.COMMENT_CREATION_VIEW.supports_schema_def
            if is_view
            else self.COMMENT_CREATION_TABLE.supports_schema_def
        )
        return [
            self._build_column_def(
                column,
                column_descriptions=column_descriptions,
                engine_supports_schema_comments=engine_supports_schema_comments,
                col_type=None if is_view else kind,  # don't include column data type for views
            )
            for column, kind in target_columns_to_types.items()
        ]

    def _build_column_def(
        self,
        col_name: str,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        engine_supports_schema_comments: bool = False,
        col_type: t.Optional[exp.DATA_TYPE] = None,
        nested_names: t.List[str] = [],
    ) -> exp.ColumnDef:
        return exp.ColumnDef(
            this=exp.to_identifier(col_name),
            kind=col_type,
            constraints=(
                self._build_col_comment_exp(col_name, column_descriptions)
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
                    kind=exp.CommentColumnConstraint(
                        this=exp.Literal.string(self._truncate_column_comment(comment))
                    )
                )
            ]
        return []

    def _create_table_from_source_queries(
        self,
        table_name: TableName,
        source_queries: t.List[SourceQuery],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        exists: bool = True,
        replace: bool = False,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        table_kind: t.Optional[str] = None,
        track_rows_processed: bool = True,
        **kwargs: t.Any,
    ) -> None:
        table = exp.to_table(table_name)

        # CTAS calls do not usually include a schema expression. However, most engines
        # permit them in CTAS expressions, and they allow us to register all column comments
        # in a single call rather than in a separate comment command call for each column.
        #
        # This block conditionally builds a schema expression with column comments if the engine
        # supports it and we have columns_to_types. column_to_types is required because the
        # schema expression must include at least column name, data type, and the comment -
        # for example, `(colname INTEGER COMMENT 'comment')`.
        #
        # column_to_types will be available when loading from a DataFrame (by converting from
        # pandas to SQL types), when a model is "annotated" by explicitly specifying column
        # types, and for evaluation methods like `LogicalReplaceQueryMixin.replace_query()`
        # calls and SCD Type 2 model calls.
        schema = None
        target_columns_to_types_known = target_columns_to_types and columns_to_types_all_known(
            target_columns_to_types
        )
        if (
            column_descriptions
            and target_columns_to_types_known
            and self.COMMENT_CREATION_TABLE.is_in_schema_def_ctas
            and self.comments_enabled
        ):
            schema = self._build_schema_exp(table, target_columns_to_types, column_descriptions)  # type: ignore

        with self.transaction(condition=len(source_queries) > 1):
            for i, source_query in enumerate(source_queries):
                with source_query as query:
                    if target_columns_to_types and target_columns_to_types_known:
                        query = self._order_projections_and_filter(
                            query, target_columns_to_types, coerce_types=True
                        )
                    if i == 0:
                        self._create_table(
                            schema if schema else table,
                            query,
                            target_columns_to_types=target_columns_to_types,
                            exists=exists,
                            replace=replace,
                            table_description=table_description,
                            table_kind=table_kind,
                            track_rows_processed=track_rows_processed,
                            **kwargs,
                        )
                    else:
                        self._insert_append_query(
                            table_name,
                            query,
                            target_columns_to_types or self.columns(table),
                            track_rows_processed=track_rows_processed,
                        )

        # Register comments with commands if the engine supports comments and we weren't able to
        # register them with the CTAS call's schema expression.
        if (
            table_description
            and self.COMMENT_CREATION_TABLE.is_comment_command_only
            and self.comments_enabled
        ):
            self._create_table_comment(table_name, table_description)
        if column_descriptions and schema is None and self.comments_enabled:
            self._create_column_comments(table_name, column_descriptions)

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
        self.execute(
            self._build_create_table_exp(
                table_name_or_schema,
                expression=expression,
                exists=exists,
                replace=replace,
                target_columns_to_types=target_columns_to_types,
                table_description=(
                    table_description
                    if self.COMMENT_CREATION_TABLE.supports_schema_def and self.comments_enabled
                    else None
                ),
                table_kind=table_kind,
                **kwargs,
            ),
            track_rows_processed=track_rows_processed,
        )
        # Extract table name to clear cache
        table_name = (
            table_name_or_schema.this
            if isinstance(table_name_or_schema, exp.Schema)
            else table_name_or_schema
        )
        self._clear_data_object_cache(table_name)

    def _build_create_table_exp(
        self,
        table_name_or_schema: t.Union[exp.Schema, TableName],
        expression: t.Optional[exp.Expr],
        exists: bool = True,
        replace: bool = False,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        table_kind: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> exp.Create:
        exists = False if replace else exists
        catalog_name = None
        if not isinstance(table_name_or_schema, exp.Schema):
            table_name_or_schema = exp.to_table(table_name_or_schema)
            catalog_name = table_name_or_schema.catalog
        else:
            if isinstance(table_name_or_schema.this, exp.Table):
                catalog_name = table_name_or_schema.this.catalog

        properties = (
            self._build_table_properties_exp(
                **kwargs,
                catalog_name=catalog_name,
                target_columns_to_types=target_columns_to_types,
                table_description=table_description,
                table_kind=table_kind,
            )
            if kwargs or table_description
            else None
        )
        return exp.Create(
            this=table_name_or_schema,
            kind=table_kind or "TABLE",
            replace=replace,
            exists=exists,
            expression=expression,
            properties=properties,
        )

    def create_table_like(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        exists: bool = True,
        **kwargs: t.Any,
    ) -> None:
        """Create a table to store SQLMesh internal state based on the definition of another table, including any
        column attributes and indexes defined in the original table.

        Args:
            target_table_name: The name of the table to create. Can be fully qualified or just table name.
            source_table_name: The name of the table to base the new table on.
        """
        self._create_table_like(target_table_name, source_table_name, exists=exists, **kwargs)
        self._clear_data_object_cache(target_table_name)

    def clone_table(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        replace: bool = False,
        exists: bool = True,
        clone_kwargs: t.Optional[t.Dict[str, t.Any]] = None,
        **kwargs: t.Any,
    ) -> None:
        """Creates a table with the target name by cloning the source table.

        Args:
            target_table_name: The name of the table that should be created.
            source_table_name: The name of the source table that should be cloned.
            replace: Whether or not to replace an existing table.
            exists: Indicates whether to include the IF NOT EXISTS check.
        """
        if not self.SUPPORTS_CLONING:
            raise NotImplementedError(f"Engine does not support cloning: {type(self)}")

        kwargs.pop("rendered_physical_properties", None)
        self.execute(
            exp.Create(
                this=exp.to_table(target_table_name),
                kind="TABLE",
                replace=replace,
                exists=exists,
                clone=exp.Clone(
                    this=exp.to_table(source_table_name),
                    **(clone_kwargs or {}),
                ),
                **kwargs,
            )
        )
        self._clear_data_object_cache(target_table_name)

    def drop_data_object(self, data_object: DataObject, ignore_if_not_exists: bool = True) -> None:
        """Drops a data object of arbitrary type.

        Args:
            data_object: The data object to drop.
            ignore_if_not_exists: If True, no error will be raised if the data object does not exist.
        """
        if data_object.type.is_view:
            self.drop_view(data_object.to_table(), ignore_if_not_exists=ignore_if_not_exists)
        elif data_object.type.is_materialized_view:
            self.drop_view(
                data_object.to_table(), ignore_if_not_exists=ignore_if_not_exists, materialized=True
            )
        elif data_object.type.is_table:
            self.drop_table(data_object.to_table(), exists=ignore_if_not_exists)
        elif data_object.type.is_managed_table:
            self.drop_managed_table(data_object.to_table(), exists=ignore_if_not_exists)
        else:
            raise SQLMeshError(
                f"Can't drop data object '{data_object.to_table().sql(dialect=self.dialect)}' of type '{data_object.type.value}'"
            )

    def drop_table(self, table_name: TableName, exists: bool = True, **kwargs: t.Any) -> None:
        """Drops a table.

        Args:
            table_name: The name of the table to drop.
            exists: If exists, defaults to True.
        """
        self._drop_object(name=table_name, exists=exists, **kwargs)

    def drop_managed_table(self, table_name: TableName, exists: bool = True) -> None:
        """Drops a managed table.

        Args:
            table_name: The name of the table to drop.
            exists: If exists, defaults to True.
        """
        raise NotImplementedError(f"Engine does not support managed tables: {type(self)}")

    def _drop_object(
        self,
        name: TableName | SchemaName,
        exists: bool = True,
        kind: str = "TABLE",
        cascade: bool = False,
        **drop_args: t.Any,
    ) -> None:
        """Drops an object.

        An object could be a DATABASE, SCHEMA, VIEW, TABLE, DYNAMIC TABLE, TEMPORARY TABLE etc depending on the :kind.

        Args:
            name: The name of the table to drop.
            exists: If exists, defaults to True.
            kind: What kind of object to drop. Defaults to TABLE
            cascade: Whether or not to DROP ... CASCADE.
                Note that this is ignored for :kind's that are not present in self.SUPPORTED_DROP_CASCADE_OBJECT_KINDS
            **drop_args: Any extra arguments to set on the Drop expression
        """
        if cascade and kind.upper() in self.SUPPORTED_DROP_CASCADE_OBJECT_KINDS:
            drop_args["cascade"] = cascade

        self.execute(exp.Drop(this=exp.to_table(name), kind=kind, exists=exists, **drop_args))
        self._clear_data_object_cache(name)

    def get_alter_operations(
        self,
        current_table_name: TableName,
        target_table_name: TableName,
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterOperation]:
        """
        Determines the alter statements needed to change the current table into the structure of the target table.
        """
        return t.cast(
            t.List[TableAlterOperation],
            self.schema_differ.compare_columns(
                current_table_name,
                self.columns(current_table_name),
                self.columns(target_table_name),
                ignore_destructive=ignore_destructive,
                ignore_additive=ignore_additive,
            ),
        )

    def alter_table(
        self,
        alter_expressions: t.Union[t.List[exp.Alter], t.List[TableAlterOperation]],
    ) -> None:
        """
        Performs the alter statements to change the current table into the structure of the target table.
        """
        with self.transaction():
            for alter_expression in [
                x.expression if isinstance(x, TableAlterOperation) else x for x in alter_expressions
            ]:
                self.execute(alter_expression)

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
        """Create a view with a query or dataframe.

        If a dataframe is passed in, it will be converted into a literal values statement.
        This should only be done if the dataframe is very small!

        Args:
            view_name: The view name.
            query_or_df: A query or dataframe.
            target_columns_to_types: Columns to use in the view statement.
            replace: Whether or not to replace an existing view defaults to True.
            materialized: Whether to create a a materialized view. Only used for engines that support this feature.
            materialized_properties: Optional materialized view properties to add to the view.
            table_description: Optional table description from MODEL DDL.
            column_descriptions: Optional column descriptions from model query.
            view_properties: Optional view properties to add to the view.
            create_kwargs: Additional kwargs to pass into the Create expression
        """
        import pandas as pd

        if materialized_properties and not materialized:
            raise SQLMeshError("Materialized properties are only supported for materialized views")

        query_or_df = self._native_df_to_pandas_df(query_or_df)

        if isinstance(query_or_df, pd.DataFrame):
            values: t.List[t.Tuple[t.Any, ...]] = list(
                query_or_df.itertuples(index=False, name=None)
            )
            target_columns_to_types, source_columns = self._columns_to_types(
                query_or_df, target_columns_to_types, source_columns
            )
            if not target_columns_to_types:
                raise SQLMeshError("columns_to_types must be provided for dataframes")
            source_columns_to_types = get_source_columns_to_types(
                target_columns_to_types, source_columns
            )
            query_or_df = self._values_to_sql(
                values,
                source_columns_to_types,
                batch_start=0,
                batch_end=len(values),
            )

        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            target_columns_to_types,
            batch_size=0,
            target_table=view_name,
            source_columns=source_columns,
        )
        if len(source_queries) != 1:
            raise SQLMeshError("Only one source query is supported for creating views")

        schema: t.Union[exp.Table, exp.Schema] = exp.to_table(view_name)
        if target_columns_to_types:
            schema = self._build_schema_exp(
                exp.to_table(view_name),
                target_columns_to_types,
                column_descriptions,
                is_view=True,
                materialized=materialized,
            )

        properties = create_kwargs.pop("properties", None)
        if not properties:
            properties = exp.Properties(expressions=[])

        if view_properties:
            table_type = self._pop_creatable_type_from_properties(view_properties)
            if table_type:
                properties.append("expressions", table_type)

        if materialized and self.SUPPORTS_MATERIALIZED_VIEWS:
            properties.append("expressions", exp.MaterializedProperty())

            if not self.SUPPORTS_MATERIALIZED_VIEW_SCHEMA and isinstance(schema, exp.Schema):
                schema = schema.this

        if not self.SUPPORTS_VIEW_SCHEMA and isinstance(schema, exp.Schema):
            schema = schema.this

        if materialized_properties:
            partitioned_by = materialized_properties.pop("partitioned_by", None)
            clustered_by = materialized_properties.pop("clustered_by", None)
            if (
                partitioned_by
                and (
                    partitioned_by_prop := self._build_partitioned_by_exp(
                        partitioned_by, **materialized_properties
                    )
                )
                is not None
            ):
                materialized_properties["catalog_name"] = exp.to_table(view_name).catalog
                properties.append("expressions", partitioned_by_prop)
            if (
                clustered_by
                and (
                    clustered_by_prop := self._build_clustered_by_exp(
                        clustered_by, **materialized_properties
                    )
                )
                is not None
            ):
                properties.append("expressions", clustered_by_prop)

        create_view_properties = self._build_view_properties_exp(
            view_properties,
            (
                table_description
                if self.COMMENT_CREATION_VIEW.supports_schema_def and self.comments_enabled
                else None
            ),
            physical_cluster=create_kwargs.pop("physical_cluster", None),
        )
        if create_view_properties:
            for view_property in create_view_properties.expressions:
                # Small hack to make sure SECURE goes at the beginning before materialized as required by Snowflake
                if isinstance(view_property, exp.SecureProperty):
                    properties.set("expressions", view_property, index=0, overwrite=False)
                else:
                    properties.append("expressions", view_property)

        if properties.expressions:
            create_kwargs["properties"] = properties

        if replace:
            self.drop_data_object_on_type_mismatch(
                self.get_data_object(view_name),
                DataObjectType.VIEW if not materialized else DataObjectType.MATERIALIZED_VIEW,
            )

        with source_queries[0] as query:
            self.execute(
                exp.Create(
                    this=schema,
                    kind="VIEW",
                    replace=replace,
                    expression=query,
                    **create_kwargs,
                ),
                quote_identifiers=self.QUOTE_IDENTIFIERS_IN_VIEWS,
            )

        self._clear_data_object_cache(view_name)

        # Register table comment with commands if the engine doesn't support doing it in CREATE
        if (
            table_description
            and self.COMMENT_CREATION_VIEW.is_comment_command_only
            and self.comments_enabled
        ):
            self._create_table_comment(view_name, table_description, "VIEW")
        # Register column comments with commands if the engine doesn't support doing it in
        # CREATE or we couldn't do it in the CREATE schema definition because we don't have
        # columns_to_types
        if (
            column_descriptions
            and (
                self.COMMENT_CREATION_VIEW.is_comment_command_only
                or (
                    self.COMMENT_CREATION_VIEW.is_in_schema_def_and_commands
                    and not target_columns_to_types
                )
            )
            and self.comments_enabled
        ):
            self._create_column_comments(view_name, column_descriptions, "VIEW", materialized)

    @set_catalog()
    def create_schema(
        self,
        schema_name: SchemaName,
        ignore_if_exists: bool = True,
        warn_on_error: bool = True,
        properties: t.Optional[t.List[exp.Expr]] = None,
    ) -> None:
        properties = properties or []
        return self._create_schema(
            schema_name=schema_name,
            ignore_if_exists=ignore_if_exists,
            warn_on_error=warn_on_error,
            properties=properties,
            kind="SCHEMA",
        )

    def _create_schema(
        self,
        schema_name: SchemaName,
        ignore_if_exists: bool,
        warn_on_error: bool,
        properties: t.List[exp.Expr],
        kind: str,
    ) -> None:
        """Create a schema from a name or qualified table name."""
        try:
            self.execute(
                exp.Create(
                    this=to_schema(schema_name),
                    kind=kind,
                    exists=ignore_if_exists,
                    properties=exp.Properties(  # this renders as '' (empty string) if expressions is empty
                        expressions=properties
                    ),
                )
            )
        except Exception as e:
            if not warn_on_error:
                raise
            logger.warning("Failed to create %s '%s': %s", kind.lower(), schema_name, e)

    def drop_schema(
        self,
        schema_name: SchemaName,
        ignore_if_not_exists: bool = True,
        cascade: bool = False,
        **drop_args: t.Dict[str, exp.Expr],
    ) -> None:
        return self._drop_object(
            name=schema_name,
            exists=ignore_if_not_exists,
            kind="SCHEMA",
            cascade=cascade,
            **drop_args,
        )

    def drop_view(
        self,
        view_name: TableName,
        ignore_if_not_exists: bool = True,
        materialized: bool = False,
        **kwargs: t.Any,
    ) -> None:
        """Drop a view."""
        self._drop_object(
            name=view_name,
            exists=ignore_if_not_exists,
            kind="VIEW",
            materialized=materialized and self.SUPPORTS_MATERIALIZED_VIEWS,
            **kwargs,
        )

    def create_catalog(self, catalog_name: str | exp.Identifier) -> None:
        return self._create_catalog(exp.parse_identifier(catalog_name, dialect=self.dialect))

    def _create_catalog(self, catalog_name: exp.Identifier) -> None:
        raise SQLMeshError(
            f"Unable to create catalog '{catalog_name.sql(dialect=self.dialect)}' as automatic catalog management is not implemented in the {self.dialect} engine."
        )

    def drop_catalog(self, catalog_name: str | exp.Identifier) -> None:
        return self._drop_catalog(exp.parse_identifier(catalog_name, dialect=self.dialect))

    def _drop_catalog(self, catalog_name: exp.Identifier) -> None:
        raise SQLMeshError(
            f"Unable to drop catalog '{catalog_name.sql(dialect=self.dialect)}' as automatic catalog management is not implemented in the {self.dialect} engine."
        )

    def columns(
        self, table_name: TableName, include_pseudo_columns: bool = False
    ) -> t.Dict[str, exp.DataType]:
        """Fetches column names and types for the target table."""
        self.execute(exp.Describe(this=exp.to_table(table_name), kind="TABLE"))
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

    def table_exists(self, table_name: TableName) -> bool:
        table = exp.to_table(table_name)
        data_object_cache_key = _get_data_object_cache_key(table.catalog, table.db, table.name)
        if data_object_cache_key in self._data_object_cache:
            logger.debug("Table existence cache hit: %s", data_object_cache_key)
            return self._data_object_cache[data_object_cache_key] is not None

        try:
            self.execute(exp.Describe(this=table, kind="TABLE"))
            return True
        except Exception:
            return False

    def delete_from(self, table_name: TableName, where: t.Union[str, exp.Expr]) -> None:
        self.execute(exp.delete(table_name, where))

    def insert_append(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        track_rows_processed: bool = True,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> None:
        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            target_columns_to_types,
            target_table=table_name,
            source_columns=source_columns,
        )
        self._insert_append_source_queries(
            table_name, source_queries, target_columns_to_types, track_rows_processed
        )

    def _insert_append_source_queries(
        self,
        table_name: TableName,
        source_queries: t.List[SourceQuery],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        track_rows_processed: bool = True,
    ) -> None:
        with self.transaction(condition=len(source_queries) > 0):
            target_columns_to_types = target_columns_to_types or self.columns(table_name)
            for source_query in source_queries:
                with source_query as query:
                    self._insert_append_query(
                        table_name,
                        query,
                        target_columns_to_types,
                        track_rows_processed=track_rows_processed,
                    )

    def _insert_append_query(
        self,
        table_name: TableName,
        query: Query,
        target_columns_to_types: t.Dict[str, exp.DataType],
        order_projections: bool = True,
        track_rows_processed: bool = True,
    ) -> None:
        if order_projections:
            query = self._order_projections_and_filter(query, target_columns_to_types)
        self.execute(
            exp.insert(query, table_name, columns=list(target_columns_to_types)),
            track_rows_processed=track_rows_processed,
        )

    def insert_overwrite_by_partition(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        partitioned_by: t.List[exp.Expr],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> None:
        if self.INSERT_OVERWRITE_STRATEGY.is_insert_overwrite:
            target_table = exp.to_table(table_name)
            source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
                query_or_df,
                target_columns_to_types,
                target_table=target_table,
                source_columns=source_columns,
            )
            self._insert_overwrite_by_condition(
                table_name, source_queries, target_columns_to_types=target_columns_to_types
            )
        else:
            self._replace_by_key(
                table_name,
                query_or_df,
                target_columns_to_types,
                partitioned_by,
                is_unique_key=False,
                source_columns=source_columns,
            )

    def insert_overwrite_by_time_partition(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        start: TimeLike,
        end: TimeLike,
        time_formatter: t.Callable[[TimeLike, t.Optional[t.Dict[str, exp.DataType]]], exp.Expr],
        time_column: TimeColumn | exp.Expr | str,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            target_columns_to_types,
            target_table=table_name,
            source_columns=source_columns,
        )
        if not target_columns_to_types or not columns_to_types_all_known(target_columns_to_types):
            target_columns_to_types = self.columns(table_name)
        low, high = [
            time_formatter(dt, target_columns_to_types)
            for dt in make_inclusive(start, end, self.dialect)
        ]
        if isinstance(time_column, TimeColumn):
            time_column = time_column.column
        where = exp.Between(
            this=exp.to_column(time_column) if isinstance(time_column, str) else time_column,
            low=low,
            high=high,
        )
        return self._insert_overwrite_by_time_partition(
            table_name, source_queries, target_columns_to_types, where, **kwargs
        )

    def _insert_overwrite_by_time_partition(
        self,
        table_name: TableName,
        source_queries: t.List[SourceQuery],
        target_columns_to_types: t.Dict[str, exp.DataType],
        where: exp.Condition,
        **kwargs: t.Any,
    ) -> None:
        return self._insert_overwrite_by_condition(
            table_name, source_queries, target_columns_to_types, where, **kwargs
        )

    def _values_to_sql(
        self,
        values: t.List[t.Tuple[t.Any, ...]],
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_start: int,
        batch_end: int,
        alias: str = "t",
        source_columns: t.Optional[t.List[str]] = None,
    ) -> Query:
        return select_from_values_for_batch_range(
            values=values,
            target_columns_to_types=target_columns_to_types,
            batch_start=batch_start,
            batch_end=batch_end,
            alias=alias,
            source_columns=source_columns,
        )

    def _insert_overwrite_by_condition(
        self,
        table_name: TableName,
        source_queries: t.List[SourceQuery],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        where: t.Optional[exp.Condition] = None,
        insert_overwrite_strategy_override: t.Optional[InsertOverwriteStrategy] = None,
        **kwargs: t.Any,
    ) -> None:
        table = exp.to_table(table_name)
        insert_overwrite_strategy = (
            insert_overwrite_strategy_override or self.INSERT_OVERWRITE_STRATEGY
        )
        with self.transaction(
            condition=len(source_queries) > 0 or insert_overwrite_strategy.is_delete_insert
        ):
            target_columns_to_types = target_columns_to_types or self.columns(table_name)
            for i, source_query in enumerate(source_queries):
                with source_query as query:
                    query = self._order_projections_and_filter(
                        query, target_columns_to_types, where=where
                    )
                    if i > 0 or insert_overwrite_strategy.is_delete_insert:
                        if i == 0:
                            self.delete_from(table_name, where=where or exp.true())
                        self._insert_append_query(
                            table_name,
                            query,
                            target_columns_to_types=target_columns_to_types,
                            order_projections=False,
                        )
                    elif insert_overwrite_strategy.is_merge:
                        columns = [exp.column(col) for col in target_columns_to_types]
                        when_not_matched_by_source = exp.When(
                            matched=False,
                            source=True,
                            condition=where,
                            then=exp.Delete(),
                        )
                        when_not_matched_by_target = exp.When(
                            matched=False,
                            source=False,
                            then=exp.Insert(
                                this=exp.Tuple(expressions=columns),
                                expression=exp.Tuple(expressions=columns),
                            ),
                        )
                        self._merge(
                            target_table=table_name,
                            query=query,
                            on=exp.false(),
                            whens=exp.Whens(
                                expressions=[when_not_matched_by_source, when_not_matched_by_target]
                            ),
                        )
                    else:
                        insert_exp = exp.insert(
                            query,
                            table,
                            columns=(
                                list(target_columns_to_types)
                                if not insert_overwrite_strategy.is_replace_where
                                else None
                            ),
                            overwrite=insert_overwrite_strategy.is_insert_overwrite,
                        )
                        if insert_overwrite_strategy.is_replace_where:
                            insert_exp.set("where", where or exp.true())
                        self.execute(insert_exp, track_rows_processed=True)

    def update_table(
        self,
        table_name: TableName,
        properties: t.Dict[str, t.Any],
        where: t.Optional[str | exp.Condition] = None,
    ) -> None:
        self.execute(exp.update(table_name, properties, where=where))

    def _merge(
        self,
        target_table: TableName,
        query: Query,
        on: exp.Expr,
        whens: exp.Whens,
    ) -> None:
        this = exp.alias_(exp.to_table(target_table), alias=MERGE_TARGET_ALIAS, table=True)
        using = exp.alias_(
            exp.Subquery(this=query), alias=MERGE_SOURCE_ALIAS, copy=False, table=True
        )
        self.execute(
            exp.Merge(this=this, using=using, on=on, whens=whens), track_rows_processed=True
        )

    def scd_type_2_by_time(
        self,
        target_table: TableName,
        source_table: QueryOrDF,
        unique_key: t.Sequence[exp.Expr],
        valid_from_col: exp.Column,
        valid_to_col: exp.Column,
        execution_time: t.Union[TimeLike, exp.Column],
        updated_at_col: exp.Column,
        invalidate_hard_deletes: bool = True,
        updated_at_as_valid_from: bool = False,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        truncate: bool = False,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        self._scd_type_2(
            target_table=target_table,
            source_table=source_table,
            unique_key=unique_key,
            valid_from_col=valid_from_col,
            valid_to_col=valid_to_col,
            execution_time=execution_time,
            updated_at_col=updated_at_col,
            invalidate_hard_deletes=invalidate_hard_deletes,
            updated_at_as_valid_from=updated_at_as_valid_from,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            column_descriptions=column_descriptions,
            truncate=truncate,
            source_columns=source_columns,
            **kwargs,
        )

    def scd_type_2_by_column(
        self,
        target_table: TableName,
        source_table: QueryOrDF,
        unique_key: t.Sequence[exp.Expr],
        valid_from_col: exp.Column,
        valid_to_col: exp.Column,
        execution_time: t.Union[TimeLike, exp.Column],
        check_columns: t.Union[exp.Star, t.Sequence[exp.Expr]],
        invalidate_hard_deletes: bool = True,
        execution_time_as_valid_from: bool = False,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        truncate: bool = False,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        self._scd_type_2(
            target_table=target_table,
            source_table=source_table,
            unique_key=unique_key,
            valid_from_col=valid_from_col,
            valid_to_col=valid_to_col,
            execution_time=execution_time,
            check_columns=check_columns,
            target_columns_to_types=target_columns_to_types,
            invalidate_hard_deletes=invalidate_hard_deletes,
            execution_time_as_valid_from=execution_time_as_valid_from,
            table_description=table_description,
            column_descriptions=column_descriptions,
            truncate=truncate,
            source_columns=source_columns,
            **kwargs,
        )

    def _scd_type_2(
        self,
        target_table: TableName,
        source_table: QueryOrDF,
        unique_key: t.Sequence[exp.Expr],
        valid_from_col: exp.Column,
        valid_to_col: exp.Column,
        execution_time: t.Union[TimeLike, exp.Column],
        invalidate_hard_deletes: bool = True,
        updated_at_col: t.Optional[exp.Column] = None,
        check_columns: t.Optional[t.Union[exp.Star, t.Sequence[exp.Expr]]] = None,
        updated_at_as_valid_from: bool = False,
        execution_time_as_valid_from: bool = False,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        truncate: bool = False,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        def remove_managed_columns(
            cols_to_types: t.Dict[str, exp.DataType],
        ) -> t.Dict[str, exp.DataType]:
            return {
                k: v for k, v in cols_to_types.items() if k not in {valid_from_name, valid_to_name}
            }

        valid_from_name = valid_from_col.name
        valid_to_name = valid_to_col.name
        target_columns_to_types = target_columns_to_types or self.columns(target_table)
        if (
            valid_from_name not in target_columns_to_types
            or valid_to_name not in target_columns_to_types
            or not columns_to_types_all_known(target_columns_to_types)
        ):
            target_columns_to_types = self.columns(target_table)
        unmanaged_columns_to_types = (
            remove_managed_columns(target_columns_to_types) if target_columns_to_types else None
        )
        source_queries, unmanaged_columns_to_types = self._get_source_queries_and_columns_to_types(
            source_table,
            unmanaged_columns_to_types,
            target_table=target_table,
            batch_size=0,
            source_columns=source_columns,
        )
        updated_at_name = updated_at_col.name if updated_at_col else None
        if not target_columns_to_types:
            raise SQLMeshError(f"Could not get columns_to_types. Does {target_table} exist?")
        unmanaged_columns_to_types = unmanaged_columns_to_types or remove_managed_columns(
            target_columns_to_types
        )
        if not unique_key:
            raise SQLMeshError("unique_key must be provided for SCD Type 2")
        if check_columns and updated_at_col:
            raise SQLMeshError(
                "Cannot use both `check_columns` and `updated_at_name` for SCD Type 2"
            )
        if check_columns and updated_at_as_valid_from:
            raise SQLMeshError(
                "Cannot use both `check_columns` and `updated_at_as_valid_from` for SCD Type 2"
            )
        if execution_time_as_valid_from and not check_columns:
            raise SQLMeshError(
                "Cannot use `execution_time_as_valid_from` without `check_columns` for SCD Type 2"
            )
        if updated_at_name and updated_at_name not in target_columns_to_types:
            raise SQLMeshError(
                f"Column {updated_at_name} not found in {target_table}. Table must contain an `updated_at` timestamp for SCD Type 2"
            )
        time_data_type = target_columns_to_types[valid_from_name]
        select_source_columns: t.List[t.Union[str, exp.Alias]] = [
            col for col in unmanaged_columns_to_types if col != updated_at_name
        ]
        table_columns = [exp.column(c, quoted=True) for c in target_columns_to_types]
        if updated_at_name:
            select_source_columns.append(
                exp.cast(updated_at_col, time_data_type).as_(updated_at_col.this)  # type: ignore
            )

        # If a star is provided, we include all unmanaged columns in the check.
        # This unnecessarily includes unique key columns but since they are used in the join, and therefore we know
        # they are equal or not, the extra check is not a problem and we gain simplified logic here.
        # If we want to change this, then we just need to check the expressions in unique_key and pull out the
        # column names and then remove them from the unmanaged_columns
        if check_columns:
            # Handle both Star directly and [Star()] (which can happen during serialization/deserialization)
            if isinstance(seq_get(ensure_list(check_columns), 0), exp.Star):
                check_columns = [exp.column(col) for col in unmanaged_columns_to_types]
        execution_ts = (
            exp.cast(execution_time, time_data_type, dialect=self.dialect)
            if isinstance(execution_time, exp.Column)
            else to_time_column(execution_time, time_data_type, self.dialect, nullable=True)
        )
        if updated_at_as_valid_from:
            if not updated_at_col:
                raise SQLMeshError(
                    "Cannot use `updated_at_as_valid_from` without `updated_at_name` for SCD Type 2"
                )
            update_valid_from_start: t.Union[str, exp.Expr] = updated_at_col
        # If using check_columns and the user doesn't always want execution_time for valid from
        # then we only use epoch 0 if we are truncating the table and loading rows for the first time.
        # All future new rows should have execution time.
        elif check_columns and (execution_time_as_valid_from or not truncate):
            update_valid_from_start = execution_ts
        else:
            update_valid_from_start = to_time_column(
                "1970-01-01 00:00:00+00:00", time_data_type, self.dialect, nullable=True
            )
        insert_valid_from_start = execution_ts if check_columns else updated_at_col  # type: ignore
        # joined._exists IS NULL is saying "if the row is deleted"
        delete_check = (
            exp.column("_exists", "joined").is_(exp.Null()) if invalidate_hard_deletes else None
        )
        prefixed_valid_to_col = valid_to_col.copy()
        prefixed_valid_to_col.this.set("this", f"t_{prefixed_valid_to_col.name}")
        prefixed_valid_from_col = valid_from_col.copy()
        prefixed_valid_from_col.this.set("this", f"t_{valid_from_col.name}")
        if check_columns:
            row_check_conditions = []
            for col in check_columns:
                col_qualified = col.copy()
                col_qualified.set("table", exp.to_identifier("joined"))

                t_col = col_qualified.copy()
                for column in t_col.find_all(exp.Column):
                    column.this.set("this", f"t_{column.name}")

                row_check_conditions.extend(
                    [
                        col_qualified.neq(t_col),
                        exp.and_(t_col.is_(exp.Null()), col_qualified.is_(exp.Null()).not_()),
                        exp.and_(t_col.is_(exp.Null()).not_(), col_qualified.is_(exp.Null())),
                    ]
                )
            row_value_check = exp.or_(*row_check_conditions)
            unique_key_conditions = []
            for key in unique_key:
                key_qualified = key.copy()
                key_qualified.set("table", exp.to_identifier("joined"))
                t_key = key_qualified.copy()
                for col in t_key.find_all(exp.Column):
                    col.this.set("this", f"t_{col.name}")
                unique_key_conditions.extend(
                    [t_key.is_(exp.Null()).not_(), key_qualified.is_(exp.Null()).not_()]
                )
            unique_key_check = exp.and_(*unique_key_conditions)
            # unique_key_check is saying "if the row is updated"
            # row_value_check is saying "if the row has changed"
            updated_row_filter = exp.and_(unique_key_check, row_value_check)
            valid_to_case_stmt = (
                exp.Case()
                .when(
                    exp.and_(
                        exp.or_(
                            delete_check,
                            updated_row_filter,
                        )
                    ),
                    execution_ts,
                )
                .else_(prefixed_valid_to_col)
                .as_(valid_to_col.this)
            )
            valid_from_case_stmt = exp.func(
                "COALESCE",
                prefixed_valid_from_col,
                update_valid_from_start,
            ).as_(valid_from_col.this)
        else:
            assert updated_at_col is not None
            updated_at_col_qualified = updated_at_col.copy()
            updated_at_col_qualified.set("table", exp.to_identifier("joined"))
            prefixed_updated_at_col = updated_at_col_qualified.copy()
            prefixed_updated_at_col.this.set("this", f"t_{updated_at_col_qualified.name}")
            updated_row_filter = updated_at_col_qualified > prefixed_updated_at_col

            valid_to_case_stmt_builder = exp.Case().when(
                updated_row_filter, updated_at_col_qualified
            )
            if delete_check:
                valid_to_case_stmt_builder = valid_to_case_stmt_builder.when(
                    delete_check, execution_ts
                )
            valid_to_case_stmt = valid_to_case_stmt_builder.else_(prefixed_valid_to_col).as_(
                valid_to_col.this
            )

            valid_from_case_stmt = (
                exp.Case()
                .when(
                    exp.and_(
                        prefixed_valid_from_col.is_(exp.Null()),
                        exp.column("_exists", "latest_deleted").is_(exp.Null()).not_(),
                    ),
                    exp.Case()
                    .when(
                        exp.column(valid_to_col.this, "latest_deleted") > updated_at_col,
                        exp.column(valid_to_col.this, "latest_deleted"),
                    )
                    .else_(updated_at_col),
                )
                .when(prefixed_valid_from_col.is_(exp.Null()), update_valid_from_start)
                .else_(prefixed_valid_from_col)
            ).as_(valid_from_col.this)

        existing_rows_query = exp.select(*table_columns, exp.true().as_("_exists")).from_(
            target_table
        )
        if truncate:
            existing_rows_query = existing_rows_query.limit(0)

        with source_queries[0] as source_query:
            prefixed_columns_to_types = []
            for column in target_columns_to_types:
                prefixed_col = exp.column(column).copy()
                prefixed_col.this.set("this", f"t_{prefixed_col.name}")
                prefixed_columns_to_types.append(prefixed_col)
            prefixed_unmanaged_columns = []
            for column in unmanaged_columns_to_types:
                prefixed_col = exp.column(column).copy()
                prefixed_col.this.set("this", f"t_{prefixed_col.name}")
                prefixed_unmanaged_columns.append(prefixed_col)
            query = (
                exp.Select()  # type: ignore
                .select(*table_columns)
                .from_("static")
                .union(
                    exp.select(*table_columns).from_("updated_rows"),
                    distinct=False,
                )
                .union(
                    exp.select(*table_columns).from_("inserted_rows"),
                    distinct=False,
                )
                .with_(
                    "source",
                    exp.select(exp.true().as_("_exists"), *select_source_columns)
                    .distinct(*unique_key)
                    .from_(
                        self.use_server_nulls_for_unmatched_after_join(source_query).subquery(  # type: ignore
                            "raw_source"
                        )
                    ),
                )
                # Historical Records that Do Not Change
                .with_(
                    "static",
                    existing_rows_query.where(valid_to_col.is_(exp.Null()).not_()),
                )
                # Latest Records that can be updated
                .with_(
                    "latest",
                    existing_rows_query.where(valid_to_col.is_(exp.Null())),
                )
                # Deleted records which can be used to determine `valid_from` for undeleted source records
                .with_(
                    "deleted",
                    exp.select(*[exp.column(col, "static") for col in target_columns_to_types])
                    .from_("static")
                    .join(
                        "latest",
                        on=exp.and_(
                            *[
                                add_table(key, "static").eq(add_table(key, "latest"))
                                for key in unique_key
                            ]
                        ),
                        join_type="left",
                    )
                    .where(exp.column(valid_to_col.this, "latest").is_(exp.Null())),
                )
                # Get the latest `valid_to` deleted record for each unique key
                .with_(
                    "latest_deleted",
                    exp.select(
                        exp.true().as_("_exists"),
                        *(part.as_(f"_key{i}") for i, part in enumerate(unique_key)),
                        exp.Max(this=valid_to_col).as_(valid_to_col.this),
                    )
                    .from_("deleted")
                    .group_by(*unique_key),
                )
                # Do a full join between latest records and source table in order to combine them together
                # MySQL doesn't support full join so going to do a left then right join and remove dups with union
                # We do a left/right and filter right on only matching to remove the need to do union distinct
                # which allows scd type 2 to be compatible with unhashable data types
                .with_(
                    "joined",
                    exp.select(
                        exp.column("_exists", table="source").as_("_exists"),
                        *(
                            exp.column(col, table="latest").as_(prefixed_columns_to_types[i].this)
                            for i, col in enumerate(target_columns_to_types)
                        ),
                        *(
                            exp.column(col, table="source").as_(col)
                            for col in unmanaged_columns_to_types
                        ),
                    )
                    .from_("latest")
                    .join(
                        "source",
                        on=exp.and_(
                            *[
                                add_table(key, "latest").eq(add_table(key, "source"))
                                for key in unique_key
                            ]
                        ),
                        join_type="left",
                    )
                    .union(
                        exp.select(
                            exp.column("_exists", table="source").as_("_exists"),
                            *(
                                exp.column(col, table="latest").as_(
                                    prefixed_columns_to_types[i].this
                                )
                                for i, col in enumerate(target_columns_to_types)
                            ),
                            *(
                                exp.column(col, table="source").as_(col)
                                for col in unmanaged_columns_to_types
                            ),
                        )
                        .from_("latest")
                        .join(
                            "source",
                            on=exp.and_(
                                *[
                                    add_table(key, "latest").eq(add_table(key, "source"))
                                    for key in unique_key
                                ]
                            ),
                            join_type="right",
                        )
                        .where(exp.column("_exists", table="latest").is_(exp.Null())),
                        distinct=False,
                    ),
                )
                # Get deleted, new, no longer current, or unchanged records
                .with_(
                    "updated_rows",
                    exp.select(
                        *(
                            exp.func(
                                "COALESCE",
                                exp.column(prefixed_unmanaged_columns[i].this, table="joined"),
                                exp.column(col, table="joined"),
                            ).as_(col)
                            for i, col in enumerate(unmanaged_columns_to_types)
                        ),
                        valid_from_case_stmt,
                        valid_to_case_stmt,
                    )
                    .from_("joined")
                    .join(
                        "latest_deleted",
                        on=exp.and_(
                            *[
                                add_table(part, "joined").eq(
                                    exp.column(f"_key{i}", "latest_deleted")
                                )
                                for i, part in enumerate(unique_key)
                            ]
                        ),
                        join_type="left",
                    ),
                )
                # Get records that have been "updated" which means inserting a new record with previous `valid_from`
                .with_(
                    "inserted_rows",
                    exp.select(
                        *unmanaged_columns_to_types,
                        insert_valid_from_start.as_(valid_from_col.this),  # type: ignore
                        to_time_column(exp.null(), time_data_type, self.dialect, nullable=True).as_(
                            valid_to_col.this
                        ),
                    )
                    .from_("joined")
                    .where(updated_row_filter),
                )
            )

            self.replace_query(
                target_table,
                self.ensure_nulls_for_unmatched_after_join(query),
                target_columns_to_types=target_columns_to_types,
                table_description=table_description,
                column_descriptions=column_descriptions,
                **kwargs,
            )

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

        if not when_matched:
            match_expressions = [
                exp.When(
                    matched=True,
                    source=False,
                    then=exp.Update(
                        expressions=[
                            exp.column(col, MERGE_TARGET_ALIAS).eq(
                                exp.column(col, MERGE_SOURCE_ALIAS)
                            )
                            for col in target_columns_to_types
                        ],
                    ),
                )
            ]
        else:
            match_expressions = when_matched.copy().expressions

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

    def rename_table(
        self,
        old_table_name: TableName,
        new_table_name: TableName,
    ) -> None:
        new_table = exp.to_table(new_table_name)
        if new_table.catalog:
            old_table = exp.to_table(old_table_name)
            catalog = old_table.catalog or self.get_current_catalog()
            if catalog != new_table.catalog:
                raise UnsupportedCatalogOperationError(
                    "Tried to rename table across catalogs which is not supported"
                )
        self._rename_table(old_table_name, new_table_name)
        self._clear_data_object_cache(old_table_name)
        self._clear_data_object_cache(new_table_name)

    def get_data_object(
        self, target_name: TableName, safe_to_cache: bool = False
    ) -> t.Optional[DataObject]:
        target_table = exp.to_table(target_name)
        existing_data_objects = self.get_data_objects(
            schema_(target_table.db, target_table.catalog),
            {target_table.name},
            safe_to_cache=safe_to_cache,
        )
        if existing_data_objects:
            return existing_data_objects[0]
        return None

    def get_data_objects(
        self,
        schema_name: SchemaName,
        object_names: t.Optional[t.Set[str]] = None,
        safe_to_cache: bool = False,
    ) -> t.List[DataObject]:
        """Lists all data objects in the target schema.

        Args:
            schema_name: The name of the schema to list data objects from.
            object_names: If provided, only return data objects with these names.
            safe_to_cache: Whether it is safe to cache the results of this call.

        Returns:
            A list of data objects in the target schema.
        """
        if object_names is not None:
            if not object_names:
                return []

            # Check cache for each object name
            target_schema = to_schema(schema_name)
            cached_objects = []
            missing_names = set()

            for name in object_names:
                cache_key = _get_data_object_cache_key(
                    target_schema.catalog, target_schema.db, name
                )
                if cache_key in self._data_object_cache:
                    logger.debug("Data object cache hit: %s", cache_key)
                    data_object = self._data_object_cache[cache_key]
                    # If the object is none, then the table was previously looked for but not found
                    if data_object:
                        cached_objects.append(data_object)
                else:
                    logger.debug("Data object cache miss: %s", cache_key)
                    missing_names.add(name)

            # Fetch missing objects from database
            if missing_names:
                object_names_list = list(missing_names)
                batches = [
                    object_names_list[i : i + self.DATA_OBJECT_FILTER_BATCH_SIZE]
                    for i in range(0, len(object_names_list), self.DATA_OBJECT_FILTER_BATCH_SIZE)
                ]

                fetched_objects = []
                fetched_object_names = set()
                for batch in batches:
                    objects = self._get_data_objects(schema_name, set(batch))
                    for obj in objects:
                        if safe_to_cache:
                            cache_key = _get_data_object_cache_key(
                                obj.catalog, obj.schema_name, obj.name
                            )
                            self._data_object_cache[cache_key] = obj
                        fetched_objects.append(obj)
                        fetched_object_names.add(obj.name)

                if safe_to_cache:
                    for missing_name in missing_names - fetched_object_names:
                        cache_key = _get_data_object_cache_key(
                            target_schema.catalog, target_schema.db, missing_name
                        )
                        self._data_object_cache[cache_key] = None

                return cached_objects + fetched_objects

            return cached_objects

        fetched_objects = self._get_data_objects(schema_name)
        if safe_to_cache:
            for obj in fetched_objects:
                cache_key = _get_data_object_cache_key(obj.catalog, obj.schema_name, obj.name)
                self._data_object_cache[cache_key] = obj
        return fetched_objects

    def fetchone(
        self,
        query: t.Union[exp.Expr, str],
        ignore_unsupported_errors: bool = False,
        quote_identifiers: bool = False,
    ) -> t.Optional[t.Tuple]:
        with self.transaction():
            self.execute(
                query,
                ignore_unsupported_errors=ignore_unsupported_errors,
                quote_identifiers=quote_identifiers,
            )
            return self.cursor.fetchone()

    def fetchall(
        self,
        query: t.Union[exp.Expr, str],
        ignore_unsupported_errors: bool = False,
        quote_identifiers: bool = False,
    ) -> t.List[t.Tuple]:
        with self.transaction():
            self.execute(
                query,
                ignore_unsupported_errors=ignore_unsupported_errors,
                quote_identifiers=quote_identifiers,
            )
            return self.cursor.fetchall()

    def _fetch_native_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> DF:
        """Fetches a DataFrame that can be either Pandas or PySpark from the cursor"""
        with self.transaction():
            self.execute(query, quote_identifiers=quote_identifiers)
            return self.cursor.fetchdf()

    def _native_df_to_pandas_df(
        self,
        query_or_df: QueryOrDF,
    ) -> t.Union[Query, pd.DataFrame]:
        """
        Take a "native" DataFrame (eg Pyspark, Bigframe, Snowpark etc) and convert it to Pandas
        """
        import pandas as pd

        if isinstance(query_or_df, (exp.Query, pd.DataFrame)):
            return query_or_df

        # EngineAdapter subclasses that have native DataFrame types should override this
        raise NotImplementedError(f"Unable to convert {type(query_or_df)} to Pandas")

    def fetchdf(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> pd.DataFrame:
        """Fetches a Pandas DataFrame from the cursor"""
        import pandas as pd

        df = self._fetch_native_df(query, quote_identifiers=quote_identifiers)
        if not isinstance(df, pd.DataFrame):
            raise NotImplementedError(
                "The cursor's `fetch_native_df` method is not returning a pandas DataFrame. Need to update `fetchdf` so a Pandas DataFrame is returned"
            )
        return df

    def fetch_pyspark_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> PySparkDataFrame:
        """Fetches a PySpark DataFrame from the cursor"""
        raise NotImplementedError(f"Engine does not support PySpark DataFrames: {type(self)}")

    @property
    def wap_enabled(self) -> bool:
        """Returns whether WAP is enabled for this engine."""
        return self._extra_config.get("wap_enabled", False)

    def wap_supported(self, table_name: TableName) -> bool:
        """Returns whether WAP for the target table is supported."""
        return False

    def wap_table_name(self, table_name: TableName, wap_id: str) -> str:
        """Returns the updated table name for the given WAP ID.

        Args:
            table_name: The name of the target table.
            wap_id: The WAP ID to prepare.

        Returns:
            The updated table name that should be used for writing.
        """
        raise NotImplementedError(f"Engine does not support WAP: {type(self)}")

    def wap_prepare(self, table_name: TableName, wap_id: str) -> str:
        """Prepares the target table for WAP and returns the updated table name.

        Args:
            table_name: The name of the target table.
            wap_id: The WAP ID to prepare.

        Returns:
            The updated table name that should be used for writing.
        """
        raise NotImplementedError(f"Engine does not support WAP: {type(self)}")

    def wap_publish(self, table_name: TableName, wap_id: str) -> None:
        """Publishes changes with the given WAP ID to the target table.

        Args:
            table_name: The name of the target table.
            wap_id: The WAP ID to publish.
        """
        raise NotImplementedError(f"Engine does not support WAP: {type(self)}")

    def sync_grants_config(
        self,
        table: exp.Table,
        grants_config: GrantsConfig,
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> None:
        """Applies the grants_config to a table authoritatively.
        It first compares the specified grants against the current grants, and then
        applies the diffs to the table by revoking and granting privileges as needed.

        Args:
            table: The table/view to apply grants to.
            grants_config: Dictionary mapping privileges to lists of grantees.
            table_type: The type of database object (TABLE, VIEW, MATERIALIZED_VIEW).
        """
        if not self.SUPPORTS_GRANTS:
            raise NotImplementedError(f"Engine does not support grants: {type(self)}")

        current_grants = self._get_current_grants_config(table)
        new_grants, revoked_grants = self._diff_grants_configs(grants_config, current_grants)
        revoke_exprs = self._revoke_grants_config_expr(table, revoked_grants, table_type)
        grant_exprs = self._apply_grants_config_expr(table, new_grants, table_type)
        dcl_exprs = revoke_exprs + grant_exprs

        if dcl_exprs:
            self.execute(dcl_exprs)

    @contextlib.contextmanager
    def transaction(
        self,
        condition: t.Optional[bool] = None,
    ) -> t.Iterator[None]:
        """A transaction context manager."""
        if (
            self._connection_pool.is_transaction_active
            or not self.SUPPORTS_TRANSACTIONS
            or (condition is not None and not condition)
        ):
            yield
            return

        if self._pre_ping:
            try:
                logger.debug("Pinging the database to check the connection")
                self.ping()
            except Exception:
                logger.info("Connection to the database was lost. Reconnecting...")
                self._connection_pool.close()

        self._connection_pool.begin()
        try:
            yield
        except Exception as e:
            self._connection_pool.rollback()
            raise e
        else:
            self._connection_pool.commit()

    @contextlib.contextmanager
    def session(self, properties: SessionProperties) -> t.Iterator[None]:
        """A session context manager."""
        if self._is_session_active():
            yield
            return

        self._begin_session(properties)
        try:
            yield
        finally:
            self._end_session()

    def _begin_session(self, properties: SessionProperties) -> t.Any:
        """Begin a new session."""

    def _end_session(self) -> None:
        """End the existing session."""

    def _is_session_active(self) -> bool:
        """Indicates whether or not a session is active."""
        return False

    def execute(
        self,
        expressions: t.Union[str, exp.Expr, t.Sequence[exp.Expr]],
        ignore_unsupported_errors: bool = False,
        quote_identifiers: bool = True,
        track_rows_processed: bool = False,
        **kwargs: t.Any,
    ) -> None:
        """Execute a sql query."""
        to_sql_kwargs = (
            {"unsupported_level": ErrorLevel.IGNORE} if ignore_unsupported_errors else {}
        )
        with self.transaction():
            for e in ensure_list(expressions):
                if isinstance(e, exp.Expr):
                    self._check_identifier_length(e)
                    sql = self._to_sql(e, quote=quote_identifiers, **to_sql_kwargs)
                else:
                    sql = t.cast(str, e)

                sql = self._attach_correlation_id(sql)

                self._log_sql(
                    sql,
                    expression=e if isinstance(e, exp.Expr) else None,
                    quote_identifiers=quote_identifiers,
                )
                self._execute(sql, track_rows_processed, **kwargs)

    def _attach_correlation_id(self, sql: str) -> str:
        if self.ATTACH_CORRELATION_ID and self.correlation_id:
            return f"/* {self.correlation_id} */ {sql}"
        return sql

    def _log_sql(
        self,
        sql: str,
        expression: t.Optional[exp.Expr] = None,
        quote_identifiers: bool = True,
    ) -> None:
        if not logger.isEnabledFor(self._execute_log_level):
            return

        sql_to_log = sql
        if expression is not None and not isinstance(expression, exp.Query):
            values = expression.find(exp.Values)
            if values:
                values.set("expressions", [exp.to_identifier("<REDACTED VALUES>")])
                sql_to_log = self._to_sql(expression, quote=quote_identifiers)

        logger.log(self._execute_log_level, "Executing SQL: %s", sql_to_log)

    def _record_execution_stats(
        self, sql: str, rowcount: t.Optional[int] = None, bytes_processed: t.Optional[int] = None
    ) -> None:
        if self._query_execution_tracker:
            self._query_execution_tracker.record_execution(sql, rowcount, bytes_processed)

    def _execute(self, sql: str, track_rows_processed: bool = False, **kwargs: t.Any) -> None:
        self.cursor.execute(sql, **kwargs)

        if (
            self.SUPPORTS_QUERY_EXECUTION_TRACKING
            and track_rows_processed
            and self._query_execution_tracker
            and self._query_execution_tracker.is_tracking()
        ):
            if (
                rowcount := getattr(self.cursor, "rowcount", None)
            ) is not None and rowcount is not None:
                try:
                    self._record_execution_stats(sql, int(rowcount))
                except (TypeError, ValueError):
                    return

    @contextlib.contextmanager
    def temp_table(
        self,
        query_or_df: QueryOrDF,
        name: TableName = "diff",
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> t.Iterator[exp.Table]:
        """A context manager for working a temp table.

        The table will be created with a random guid and cleaned up after the block.

        Args:
            query_or_df: The query or df to create a temp table for.
            name: The base name of the temp table.
            target_columns_to_types: A mapping between the column name and its data type.

        Yields:
            The table expression
        """
        name = exp.to_table(name)
        # ensure that we use default catalog if none is not specified
        if isinstance(name, exp.Table) and not name.catalog and name.db and self.default_catalog:
            name.set("catalog", exp.parse_identifier(self.default_catalog))

        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            target_columns_to_types=target_columns_to_types,
            target_table=name,
            source_columns=source_columns,
        )

        with self.transaction():
            table = self._get_temp_table(name)
            if table.db:
                self.create_schema(schema_(table.args["db"], table.args.get("catalog")))
            self._create_table_from_source_queries(
                table,
                source_queries,
                target_columns_to_types,
                exists=True,
                table_description=None,
                column_descriptions=None,
                track_rows_processed=False,
                **kwargs,
            )

            try:
                yield table
            finally:
                self.drop_table(table)

    def _table_or_view_properties_to_expressions(
        self, table_or_view_properties: t.Optional[t.Dict[str, exp.Expr]] = None
    ) -> t.List[exp.Property]:
        """Converts model properties (either physical or virtual) to a list of property expressions."""
        if not table_or_view_properties:
            return []
        return [
            exp.Property(this=key, value=value.copy())
            for key, value in table_or_view_properties.items()
        ]

    def _build_partitioned_by_exp(
        self,
        partitioned_by: t.List[exp.Expr],
        *,
        partition_interval_unit: t.Optional[IntervalUnit] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        catalog_name: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[t.Union[exp.PartitionedByProperty, exp.Property]]:
        return None

    def _build_clustered_by_exp(
        self,
        clustered_by: t.List[exp.Expr],
        **kwargs: t.Any,
    ) -> t.Optional[exp.Cluster]:
        return None

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
        """Creates a SQLGlot table properties expression for ddl."""
        properties: t.List[exp.Expr] = []

        if table_description:
            properties.append(
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        if table_properties:
            table_type = self._pop_creatable_type_from_properties(table_properties)
            properties.extend(ensure_list(table_type))

        if properties:
            return exp.Properties(expressions=properties)
        return None

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
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        if properties:
            return exp.Properties(expressions=properties)
        return None

    def _truncate_comment(self, comment: str, length: t.Optional[int]) -> str:
        return comment[:length] if length else comment

    def _truncate_table_comment(self, comment: str) -> str:
        return self._truncate_comment(comment, self.MAX_TABLE_COMMENT_LENGTH)

    def _truncate_column_comment(self, comment: str) -> str:
        return self._truncate_comment(comment, self.MAX_COLUMN_COMMENT_LENGTH)

    def _to_sql(self, expression: exp.Expr, quote: bool = True, **kwargs: t.Any) -> str:
        """
        Converts an expression to a SQL string. Has a set of default kwargs to apply, and then default
        kwargs defined for the given dialect, and then kwargs provided by the user when defining the engine
        adapter, and then finally kwargs provided by the user when calling this method.
        """
        sql_gen_kwargs = {
            "dialect": self.dialect,
            "pretty": self._pretty_sql,
            "comments": False,
            **self._sql_gen_kwargs,
            **kwargs,
        }

        expression = expression.copy()

        if quote:
            quote_identifiers(expression)

        return expression.sql(**sql_gen_kwargs, copy=False)  # type: ignore

    def _clear_data_object_cache(self, table_name: t.Optional[TableName] = None) -> None:
        """Clears the cache entry for the given table name, or clears the entire cache if table_name is None."""
        if table_name is None:
            logger.debug("Clearing entire data object cache")
            self._data_object_cache.clear()
        else:
            table = exp.to_table(table_name)
            cache_key = _get_data_object_cache_key(table.catalog, table.db, table.name)
            logger.debug("Clearing data object cache key: %s", cache_key)
            self._data_object_cache.pop(cache_key, None)

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and optionally catalog.
        """
        raise NotImplementedError()

    def _get_temp_table(
        self, table: TableName, table_only: bool = False, quoted: bool = True
    ) -> exp.Table:
        """
        Returns the name of the temp table that should be used for the given table name.
        """
        table = t.cast(exp.Table, exp.to_table(table).copy())
        table.set(
            "this", exp.to_identifier(f"__temp_{table.name}_{random_id(short=True)}", quoted=quoted)
        )

        if table_only:
            table.set("db", None)
            table.set("catalog", None)

        return table

    def _order_projections_and_filter(
        self,
        query: Query,
        target_columns_to_types: t.Dict[str, exp.DataType],
        where: t.Optional[exp.Expr] = None,
        coerce_types: bool = False,
    ) -> Query:
        if not isinstance(query, exp.Query) or (
            not where and not coerce_types and query.named_selects == list(target_columns_to_types)
        ):
            return query

        query = t.cast(exp.Query, query.copy())
        with_ = query.args.pop("with_", None)

        select_exprs: t.List[exp.Expr] = [
            exp.column(c, quoted=True) for c in target_columns_to_types
        ]
        if coerce_types and columns_to_types_all_known(target_columns_to_types):
            select_exprs = [
                exp.cast(select_exprs[i], col_tpe).as_(col, quoted=True)
                for i, (col, col_tpe) in enumerate(target_columns_to_types.items())
            ]

        query = exp.select(*select_exprs).from_(query.subquery("_subquery", copy=False), copy=False)
        if where:
            query = query.where(where, copy=False)

        if with_:
            query.set("with_", with_)

        return query

    def _truncate_table(self, table_name: TableName) -> None:
        table = exp.to_table(table_name)
        self.execute(f"TRUNCATE TABLE {table.sql(dialect=self.dialect, identify=True)}")

    def drop_data_object_on_type_mismatch(
        self, data_object: t.Optional[DataObject], expected_type: DataObjectType
    ) -> bool:
        """Drops a data object if it exists and is not of the expected type.

        Args:
            data_object: The data object to check.
            expected_type: The expected type of the data object.

        Returns:
            True if the data object was dropped, False otherwise.
        """
        if data_object is None or data_object.type == expected_type:
            return False

        logger.warning(
            "Target data object '%s' is a %s and not a %s, dropping it",
            data_object.to_table().sql(dialect=self.dialect),
            data_object.type.value,
            expected_type.value,
        )
        self.drop_data_object(data_object)
        return True

    def _replace_by_key(
        self,
        target_table: TableName,
        source_table: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]],
        key: t.Sequence[exp.Expr],
        is_unique_key: bool,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> None:
        if target_columns_to_types is None:
            target_columns_to_types = self.columns(target_table)

        temp_table = self._get_temp_table(target_table)
        key_exp = (
            exp.func("CONCAT_WS", "'__SQLMESH_DELIM__'", *key, dialect=self.dialect)
            if len(key) > 1
            else key[0]
        )
        column_names = list(target_columns_to_types or [])

        with self.transaction():
            self.ctas(
                temp_table,
                source_table,
                target_columns_to_types=target_columns_to_types,
                exists=False,
                source_columns=source_columns,
            )

            try:
                delete_query = exp.select(key_exp).from_(temp_table)
                insert_query = self._select_columns(target_columns_to_types).from_(temp_table)
                if not is_unique_key:
                    delete_query = delete_query.distinct()
                else:
                    insert_query = insert_query.distinct(*key)

                insert_statement = exp.insert(
                    insert_query,
                    target_table,
                    columns=column_names,
                )
                delete_filter = key_exp.isin(query=delete_query)

                if not self.INSERT_OVERWRITE_STRATEGY.is_replace_where:
                    self.delete_from(target_table, delete_filter)
                else:
                    insert_statement.set("where", delete_filter)
                    insert_statement.set("this", exp.to_table(target_table))

                self.execute(insert_statement, track_rows_processed=True)
            finally:
                self.drop_table(temp_table)

    def _build_create_comment_table_exp(
        self, table: exp.Table, table_comment: str, table_kind: str
    ) -> exp.Comment | str:
        return exp.Comment(
            this=table,
            kind=table_kind,
            expression=exp.Literal.string(self._truncate_table_comment(table_comment)),
        )

    def _create_table_comment(
        self, table_name: TableName, table_comment: str, table_kind: str = "TABLE"
    ) -> None:
        table = exp.to_table(table_name)

        try:
            self.execute(self._build_create_comment_table_exp(table, table_comment, table_kind))
        except Exception:
            logger.warning(
                f"Table comment for '{table.alias_or_name}' not registered - this may be due to limited permissions",
                exc_info=True,
            )

    def _build_create_comment_column_exp(
        self, table: exp.Table, column_name: str, column_comment: str, table_kind: str = "TABLE"
    ) -> exp.Comment | str:
        return exp.Comment(
            this=exp.column(column_name, *reversed(table.parts)),  # type: ignore
            kind="COLUMN",
            expression=exp.Literal.string(self._truncate_column_comment(column_comment)),
        )

    def _create_column_comments(
        self,
        table_name: TableName,
        column_comments: t.Dict[str, str],
        table_kind: str = "TABLE",
        materialized_view: bool = False,
    ) -> None:
        table = exp.to_table(table_name)

        for col, comment in column_comments.items():
            try:
                self.execute(self._build_create_comment_column_exp(table, col, comment, table_kind))
            except Exception:
                logger.warning(
                    f"Column comments for column '{col}' in table '{table.alias_or_name}' not registered - this may be due to limited permissions",
                    exc_info=True,
                )

    def _create_table_like(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        exists: bool,
        **kwargs: t.Any,
    ) -> None:
        self.create_table(target_table_name, self.columns(source_table_name), exists=exists)

    def _rename_table(
        self,
        old_table_name: TableName,
        new_table_name: TableName,
    ) -> None:
        self.execute(exp.rename_table(old_table_name, new_table_name))

    def ensure_nulls_for_unmatched_after_join(
        self,
        query: Query,
    ) -> Query:
        return query

    def use_server_nulls_for_unmatched_after_join(
        self,
        query: Query,
    ) -> Query:
        return query

    def ping(self) -> None:
        try:
            self._execute(exp.select("1").sql(dialect=self.dialect))
        finally:
            self._connection_pool.close_cursor()

    @classmethod
    def _select_columns(
        cls, columns: t.Iterable[str], source_columns: t.Optional[t.List[str]] = None
    ) -> exp.Select:
        return exp.select(
            *(
                exp.column(c, quoted=True)
                if c in (source_columns or columns)
                else exp.alias_(exp.Null(), c, quoted=True)
                for c in columns
            )
        )

    def _check_identifier_length(self, expression: exp.Expr) -> None:
        if self.MAX_IDENTIFIER_LENGTH is None or not isinstance(expression, exp.DDL):
            return

        for identifier in expression.find_all(exp.Identifier):
            name = identifier.name
            name_length = len(name)
            if name_length > self.MAX_IDENTIFIER_LENGTH:
                raise SQLMeshError(
                    f"Identifier name '{name}' (length {name_length}) exceeds {self.dialect.capitalize()}'s max identifier limit of {self.MAX_IDENTIFIER_LENGTH} characters"
                )

    def get_table_last_modified_ts(self, table_names: t.List[TableName]) -> t.List[int]:
        raise NotImplementedError()

    @classmethod
    def _diff_grants_configs(
        cls, new_config: GrantsConfig, old_config: GrantsConfig
    ) -> t.Tuple[GrantsConfig, GrantsConfig]:
        """Compute additions and removals between two grants configurations.

        This method compares new (desired) and old (current) GrantsConfigs case-insensitively
        for both privilege keys and grantees, while preserving original casing
        in the output GrantsConfigs.

        Args:
            new_config: Desired grants configuration (specified by the user).
            old_config: Current grants configuration (returned by the database).

        Returns:
            A tuple of (additions, removals) GrantsConfig where:
            - additions contains privileges/grantees present in new_config but not in old_config
            - additions uses keys and grantee strings from new_config (user-specified casing)
            - removals contains privileges/grantees present in old_config but not in new_config
            - removals uses keys and grantee strings from old_config (database-returned casing)

        Notes:
            - Comparison is case-insensitive using casefold(); original casing is preserved in results.
            - Overlapping grantees (case-insensitive) are excluded from the results.
        """

        def _diffs(config1: GrantsConfig, config2: GrantsConfig) -> GrantsConfig:
            diffs: GrantsConfig = {}
            cf_config2 = {k.casefold(): {g.casefold() for g in v} for k, v in config2.items()}
            for key, grantees in config1.items():
                cf_key = key.casefold()

                # Missing key (add all grantees)
                if cf_key not in cf_config2:
                    diffs[key] = grantees.copy()
                    continue

                # Include only grantees not in config2
                cf_grantees2 = cf_config2[cf_key]
                diff_grantees = []
                for grantee in grantees:
                    if grantee.casefold() not in cf_grantees2:
                        diff_grantees.append(grantee)
                if diff_grantees:
                    diffs[key] = diff_grantees
            return diffs

        return _diffs(new_config, old_config), _diffs(old_config, new_config)

    def _get_current_grants_config(self, table: exp.Table) -> GrantsConfig:
        """Returns current grants for a table as a dictionary.

        This method queries the database and returns the current grants/permissions
        for the given table, parsed into a dictionary format. The it handles
        case-insensitive comparison between these current grants and the desired
        grants from model configuration.

        Args:
            table: The table/view to query grants for.

        Returns:
            Dictionary mapping permissions to lists of grantees. Permission names
            should be returned as the database provides them (typically uppercase
            for standard SQL permissions, but engine-specific roles may vary).

        Raises:
            NotImplementedError: If the engine does not support grants.
        """
        if not self.SUPPORTS_GRANTS:
            raise NotImplementedError(f"Engine does not support grants: {type(self)}")
        raise NotImplementedError("Subclass must implement get_current_grants")

    def _apply_grants_config_expr(
        self,
        table: exp.Table,
        grants_config: GrantsConfig,
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> t.List[exp.Expr]:
        """Returns SQLGlot Grant expressions to apply grants to a table.

        Args:
            table: The table/view to grant permissions on.
            grants_config: Dictionary mapping permissions to lists of grantees.
            table_type: The type of database object (TABLE, VIEW, MATERIALIZED_VIEW).

        Returns:
            List of SQLGlot expressions for grant operations.

        Raises:
            NotImplementedError: If the engine does not support grants.
        """
        if not self.SUPPORTS_GRANTS:
            raise NotImplementedError(f"Engine does not support grants: {type(self)}")
        raise NotImplementedError("Subclass must implement _apply_grants_config_expr")

    def _revoke_grants_config_expr(
        self,
        table: exp.Table,
        grants_config: GrantsConfig,
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> t.List[exp.Expr]:
        """Returns SQLGlot expressions to revoke grants from a table.

        Args:
            table: The table/view to revoke permissions from.
            grants_config: Dictionary mapping permissions to lists of grantees.
            table_type: The type of database object (TABLE, VIEW, MATERIALIZED_VIEW).

        Returns:
            List of SQLGlot expressions for revoke operations.

        Raises:
            NotImplementedError: If the engine does not support grants.
        """
        if not self.SUPPORTS_GRANTS:
            raise NotImplementedError(f"Engine does not support grants: {type(self)}")
        raise NotImplementedError("Subclass must implement _revoke_grants_config_expr")


class EngineAdapterWithIndexSupport(EngineAdapter):
    SUPPORTS_INDEXES = True


def _decoded_str(value: t.Union[str, bytes]) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _get_data_object_cache_key(catalog: t.Optional[str], schema_name: str, object_name: str) -> str:
    """Returns a cache key for a data object based on its fully qualified name."""
    catalog = f"{catalog}." if catalog else ""
    return f"{catalog}{schema_name}.{object_name}"
