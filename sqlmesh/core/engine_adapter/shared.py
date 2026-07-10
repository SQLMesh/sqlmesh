from __future__ import annotations

import functools
import inspect
import logging
import types
import typing as t
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pydantic import Field
from sqlglot import exp

from sqlmesh.core.dialect import to_schema
from sqlmesh.utils.errors import UnsupportedCatalogOperationError, SQLMeshError
from sqlmesh.utils.pydantic import PydanticModel

if t.TYPE_CHECKING:
    from sqlmesh.core.engine_adapter._typing import Query
    from sqlmesh.core.engine_adapter.base import EngineAdapter


logger = logging.getLogger(__name__)


class DataObjectType(str, Enum):
    UNKNOWN = "unknown"
    TABLE = "table"
    VIEW = "view"
    MATERIALIZED_VIEW = "materialized_view"
    MANAGED_TABLE = "managed_table"

    @property
    def is_unknown(self) -> bool:
        return self == DataObjectType.UNKNOWN

    @property
    def is_table(self) -> bool:
        return self == DataObjectType.TABLE

    @property
    def is_view(self) -> bool:
        return self == DataObjectType.VIEW

    @property
    def is_materialized_view(self) -> bool:
        return self == DataObjectType.MATERIALIZED_VIEW

    @property
    def is_managed_table(self) -> bool:
        return self == DataObjectType.MANAGED_TABLE

    @classmethod
    def from_str(cls, s: str) -> DataObjectType:
        s = s.lower()
        if s == "table":
            return DataObjectType.TABLE
        if s == "view":
            return DataObjectType.VIEW
        if s == "materialized_view":
            return DataObjectType.MATERIALIZED_VIEW
        if s == "managed_table":
            return DataObjectType.MANAGED_TABLE
        return DataObjectType.UNKNOWN


class CommentCreationTable(Enum):
    """
    Enum for SQL engine TABLE comment support.

    UNSUPPORTED = no comments at all
    IN_SCHEMA_DEF_CTAS = comments can be registered in CREATE schema definitions, including CTAS calls
    IN_SCHEMA_DEF_NO_CTAS = comments can be registered in CREATE schema definitions, excluding CTAS calls
    COMMENT_COMMAND_ONLY = comments can only be registered via a post-creation command like `COMMENT` or `ALTER`
    """

    UNSUPPORTED = 1
    IN_SCHEMA_DEF_CTAS = 2
    IN_SCHEMA_DEF_NO_CTAS = 3
    COMMENT_COMMAND_ONLY = 4

    @property
    def is_unsupported(self) -> bool:
        return self == CommentCreationTable.UNSUPPORTED

    @property
    def is_in_schema_def_ctas(self) -> bool:
        return self == CommentCreationTable.IN_SCHEMA_DEF_CTAS

    @property
    def is_in_schema_def_no_ctas(self) -> bool:
        return self == CommentCreationTable.IN_SCHEMA_DEF_NO_CTAS

    @property
    def is_comment_command_only(self) -> bool:
        return self == CommentCreationTable.COMMENT_COMMAND_ONLY

    @property
    def is_supported(self) -> bool:
        return self != CommentCreationTable.UNSUPPORTED

    @property
    def supports_schema_def(self) -> bool:
        return self in (
            CommentCreationTable.IN_SCHEMA_DEF_CTAS,
            CommentCreationTable.IN_SCHEMA_DEF_NO_CTAS,
        )


class CommentCreationView(Enum):
    """
    Enum for SQL engine VIEW comment support.

    UNSUPPORTED = no comments at all
    IN_SCHEMA_DEF_AND_COMMANDS = all comments can be registered in CREATE VIEW schema definitions
                                   and in post-creation commands
    IN_SCHEMA_DEF_NO_COMMANDS = all comments can be registered in CREATE VIEW schema definitions,
                                  but not in post-creation commands
    COMMENT_COMMAND_ONLY = comments can only be registered via a post-creation command like `COMMENT` or `ALTER`
    """

    UNSUPPORTED = 1
    IN_SCHEMA_DEF_AND_COMMANDS = 2
    IN_SCHEMA_DEF_NO_COMMANDS = 3
    COMMENT_COMMAND_ONLY = 4

    @property
    def is_unsupported(self) -> bool:
        return self == CommentCreationView.UNSUPPORTED

    @property
    def is_in_schema_def_and_commands(self) -> bool:
        return self == CommentCreationView.IN_SCHEMA_DEF_AND_COMMANDS

    @property
    def is_in_schema_def_no_commands(self) -> bool:
        return self == CommentCreationView.IN_SCHEMA_DEF_NO_COMMANDS

    @property
    def is_comment_command_only(self) -> bool:
        return self == CommentCreationView.COMMENT_COMMAND_ONLY

    @property
    def is_supported(self) -> bool:
        return self != CommentCreationView.UNSUPPORTED

    @property
    def supports_schema_def(self) -> bool:
        return self in (
            CommentCreationView.IN_SCHEMA_DEF_AND_COMMANDS,
            CommentCreationView.IN_SCHEMA_DEF_NO_COMMANDS,
        )

    @property
    def supports_column_comment_commands(self) -> bool:
        return self in (
            CommentCreationView.IN_SCHEMA_DEF_AND_COMMANDS,
            CommentCreationView.COMMENT_COMMAND_ONLY,
        )


class DataObject(PydanticModel):
    catalog: t.Optional[str] = None
    schema_name: str = Field(alias="schema")
    name: str
    type: DataObjectType

    # for type=DataObjectType.Table, only if the DB supports it
    clustering_key: t.Optional[str] = None

    @property
    def is_clustered(self) -> bool:
        return bool(self.clustering_key)

    def to_table(self) -> exp.Table:
        return exp.table_(self.name, db=self.schema_name, catalog=self.catalog, quoted=True)


class CatalogSupport(Enum):
    # The engine has no concept of catalogs
    UNSUPPORTED = 1

    # The engine has a concept of catalogs, but they are isolated from each other and cannot reference each others tables
    SINGLE_CATALOG_ONLY = 2

    # The engine supports multiple catalogs but some operations require a SET CATALOG query to set the active catalog before proceeding
    REQUIRES_SET_CATALOG = 3

    # The engine supports multiple catalogs and can unambiguously target a specific catalog when performing operations (without running SET CATALOG first)
    FULL_SUPPORT = 4

    @property
    def is_unsupported(self) -> bool:
        return self == CatalogSupport.UNSUPPORTED

    @property
    def is_single_catalog_only(self) -> bool:
        return self == CatalogSupport.SINGLE_CATALOG_ONLY

    @property
    def is_requires_set_catalog(self) -> bool:
        return self == CatalogSupport.REQUIRES_SET_CATALOG

    @property
    def is_full_support(self) -> bool:
        return self == CatalogSupport.FULL_SUPPORT

    @property
    def is_multi_catalog_supported(self) -> bool:
        return self.is_requires_set_catalog or self.is_full_support


class EngineRunMode(Enum):
    SINGLE_MODE_ENGINE = 1
    STANDALONE = 2
    CLUSTER = 3
    CLOUD = 4

    @property
    def is_single_mode_engine(self) -> bool:
        return self == EngineRunMode.SINGLE_MODE_ENGINE

    @property
    def is_standalone(self) -> bool:
        return self == EngineRunMode.STANDALONE

    @property
    def is_cluster(self) -> bool:
        return self == EngineRunMode.CLUSTER

    @property
    def is_cloud(self) -> bool:
        return self == EngineRunMode.CLOUD


class InsertOverwriteStrategy(Enum):
    # First, issue a DELETE to clear the data range. Then, issue an INSERT query to insert the new data
    DELETE_INSERT = 1
    # Issue a single INSERT OVERWRITE query to replace a data range.
    INSERT_OVERWRITE = 2
    # Issue a single INSERT INTO... REPLACE WHERE query
    # Note: Replace where on Databricks requires that `spark.sql.sources.partitionOverwriteMode` be set to `static`
    REPLACE_WHERE = 3
    # Issue a single INSERT query to replace a data range. The assumption is that the query engine will transparently match partition bounds
    # and replace data rather than append to it. Trino is an example of this when `hive.insert-existing-partitions-behavior=OVERWRITE` is configured
    INTO_IS_OVERWRITE = 4
    # Do the INSERT OVERWRITE using merge since the engine doesn't support it natively
    MERGE = 5

    @property
    def is_delete_insert(self) -> bool:
        return self == InsertOverwriteStrategy.DELETE_INSERT

    @property
    def is_insert_overwrite(self) -> bool:
        return self == InsertOverwriteStrategy.INSERT_OVERWRITE

    @property
    def is_replace_where(self) -> bool:
        return self == InsertOverwriteStrategy.REPLACE_WHERE

    @property
    def is_into_is_overwrite(self) -> bool:
        return self == InsertOverwriteStrategy.INTO_IS_OVERWRITE

    @property
    def is_merge(self) -> bool:
        return self == InsertOverwriteStrategy.MERGE


class SourceQuery:
    def __init__(
        self,
        query_factory: t.Callable[[], Query],
        cleanup_func: t.Optional[t.Callable[[], None]] = None,
        transforms: t.Optional[t.List[t.Callable[[Query], Query]]] = None,
        **kwargs: t.Any,
    ) -> None:
        self.query_factory = query_factory
        self.cleanup_func = cleanup_func
        self._transforms = transforms or []

    def add_transform(self, transform: t.Callable[[Query], Query]) -> None:
        self._transforms.append(transform)

    def __enter__(self) -> Query:
        query = self.query_factory()
        for transform in self._transforms:
            query = t.cast(exp.Query, query.transform(transform))
        return query

    def __exit__(
        self,
        exc_type: t.Optional[t.Type[BaseException]],
        exc_val: t.Optional[BaseException],
        exc_tb: t.Optional[types.TracebackType],
    ) -> t.Optional[bool]:
        if self.cleanup_func:
            self.cleanup_func()
        return None


@dataclass(frozen=True)
class QueryHistoryRecord:
    """A single query SQLMesh executed, reconstructed from the engine's query history.

    Returned by `EngineAdapter.query_history` and rendered by the `sqlmesh history` command.
    """

    started_at: t.Optional[datetime]
    sql: str
    status: str  # "success", "failed", or "running"
    duration_ms: t.Optional[int] = None
    bytes_processed: t.Optional[int] = None
    rows: t.Optional[int] = None
    error: t.Optional[str] = None
    query_id: t.Optional[str] = None
    target: t.Optional[str] = None  # physical table the statement wrote to, if any


def set_catalog(override_mapping: t.Optional[t.Dict[str, CatalogSupport]] = None) -> t.Callable:
    def set_catalog_decorator(
        func: t.Callable,
        target_name: str,
        target_pos: int,
        target_type: str,
        override: t.Optional[CatalogSupport] = None,
    ) -> t.Callable:
        @functools.wraps(func)
        def internal_wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
            # Need to convert args to list in order to later do assignment to the object
            list_args = list(args)
            engine_adapter = list_args[0]
            catalog_support = override or engine_adapter.catalog_support
            # If there is full catalog support then we have nothing to do
            if catalog_support.is_full_support:
                return func(*list_args, **kwargs)

            obj, container, key = t.cast(
                t.Tuple[t.Union[str, exp.Table], t.Union[t.Dict, t.List], t.Union[int, str]],
                (
                    (kwargs.get(target_name), kwargs, target_name)
                    if kwargs.get(target_name)
                    else (list_args[target_pos], list_args, target_pos)
                ),
            )
            to_expression_func = t.cast(
                t.Callable[[t.Union[str, exp.Table]], exp.Table],
                exp.to_table if target_type == "TableName" else to_schema,
            )
            expression = to_expression_func(obj.copy() if isinstance(obj, exp.Table) else obj)
            catalog_name = expression.catalog
            if not catalog_name:
                return func(*list_args, **kwargs)

            # If we have a catalog and this engine doesn't support catalogs then we need to error
            if catalog_support.is_unsupported:
                raise UnsupportedCatalogOperationError(
                    f"{engine_adapter.dialect} does not support catalogs and a catalog was provided: {catalog_name}"
                )
            # Remove the catalog name from the argument so the engine adapter doesn't try to use it
            expression.set("catalog", None)
            container[key] = expression  # type: ignore
            if catalog_support.is_single_catalog_only:
                if catalog_name != engine_adapter._default_catalog:
                    raise SQLMeshError(
                        f"{engine_adapter.dialect} requires that all catalog operations be against a single catalog: {engine_adapter._default_catalog}. Provided catalog: {catalog_name}"
                    )
                return func(*list_args, **kwargs)
            # Set the catalog name on the engine adapter if needed
            current_catalog = engine_adapter.get_current_catalog()
            if catalog_name != current_catalog:
                engine_adapter.set_current_catalog(catalog_name)
                resp = func(*list_args, **kwargs)
                engine_adapter.set_current_catalog(current_catalog)
            else:
                resp = func(*list_args, **kwargs)
            return resp

        return internal_wrapper

    inclusion_list = {
        "_get_data_objects",
    }

    # Exclude this to avoid a circular dependency from inspecting the classproperty
    exclusion_list = {
        "can_access_spark_session",
    }

    override_mapping = override_mapping or {}

    def wrapper(cls: t.Type[EngineAdapter]) -> t.Callable:
        for name in dir(cls):
            if name in exclusion_list or (name.startswith("_") and name not in inclusion_list):
                continue
            m = getattr(cls, name)
            if inspect.isfunction(m):
                spec = inspect.getfullargspec(m)
                for i, obj_name in enumerate(spec.args):
                    obj_type = spec.annotations.get(obj_name)
                    if obj_type not in {"SchemaName", "TableName"}:
                        continue
                    setattr(
                        cls,
                        name,
                        set_catalog_decorator(
                            m,
                            target_name=obj_name,
                            target_pos=i,
                            target_type=obj_type,
                            override=override_mapping.get(name),
                        ),
                    )
        return cls

    return wrapper
