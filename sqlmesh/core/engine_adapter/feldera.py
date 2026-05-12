from __future__ import annotations

import logging
import typing as t

from sqlglot import exp

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.engine_adapter.shared import (
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
    set_catalog,
)
from sqlmesh.engines.feldera.db_api import QUERY_MIRROR_PREFIX
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    import pandas as pd

    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter.shared import SourceQuery


logger = logging.getLogger(__name__)


def _is_query_mirror_name(name: str) -> bool:
    return name.lower().startswith(QUERY_MIRROR_PREFIX)


def _view_type(state: t.Any, object_name: str) -> DataObjectType:
    is_materialized_view = getattr(state, "is_materialized_view", None)

    if callable(is_materialized_view) and is_materialized_view(object_name):
        return DataObjectType.MATERIALIZED_VIEW

    return DataObjectType.VIEW


_FELDERA_TO_EXP_TYPE: t.Dict[str, t.Any] = {
    "BOOLEAN": exp.DataType.Type.BOOLEAN,
    "TINYINT": exp.DataType.Type.TINYINT,
    "SMALLINT": exp.DataType.Type.SMALLINT,
    "INTEGER": exp.DataType.Type.INT,
    "INT": exp.DataType.Type.INT,
    "BIGINT": exp.DataType.Type.BIGINT,
    "REAL": exp.DataType.Type.FLOAT,
    "DOUBLE": exp.DataType.Type.DOUBLE,
    "DECIMAL": exp.DataType.Type.DECIMAL,
    "NUMERIC": exp.DataType.Type.DECIMAL,
    "VARCHAR": exp.DataType.Type.VARCHAR,
    "CHAR": exp.DataType.Type.CHAR,
    "DATE": exp.DataType.Type.DATE,
    "TIME": exp.DataType.Type.TIME,
    "TIMESTAMP": exp.DataType.Type.TIMESTAMP,
    "ARRAY": exp.DataType.Type.ARRAY,
}


def _feldera_type_to_exp(dtype_str: str) -> exp.DataType:
    base = dtype_str.split("(")[0].strip().upper()
    kind = _FELDERA_TO_EXP_TYPE.get(base, exp.DataType.Type.TEXT)
    return exp.DataType(this=kind)


@set_catalog()
class FelderaEngineAdapter(EngineAdapter):
    DIALECT = "feldera"
    SUPPORTS_TRANSACTIONS = False
    SUPPORTS_INDEXES = False
    SUPPORTS_MATERIALIZED_VIEWS = True
    SUPPORTS_REPLACE_TABLE = False
    COMMENT_CREATION_TABLE = CommentCreationTable.UNSUPPORTED
    COMMENT_CREATION_VIEW = CommentCreationView.UNSUPPORTED

    def _fetch_native_df(
        self,
        query: t.Union[exp.Expr, str],
        quote_identifiers: bool = False,
    ) -> pd.DataFrame:
        with self.transaction():
            self.execute(query, quote_identifiers=quote_identifiers)
            return self.cursor.fetchdf()

    def _get_data_objects(
        self,
        schema_name: SchemaName,
        object_names: t.Optional[t.Set[str]] = None,
    ) -> t.List[DataObject]:
        from feldera.pipeline import Pipeline

        connection = self.connection
        pipeline_name = to_schema(schema_name).db
        lower_object_names = {name.lower() for name in object_names} if object_names else None

        try:
            pipeline = Pipeline.get(pipeline_name, connection._client)
        except Exception:
            return []

        objects_by_name: t.Dict[str, DataObject] = {}
        for table in pipeline.tables():
            name = table.name.lower()
            if lower_object_names and name not in lower_object_names:
                continue
            objects_by_name[name] = DataObject(
                catalog=None,
                schema=pipeline_name,
                name=name,
                type=DataObjectType.TABLE,
            )

        for view in pipeline.views():
            name = view.name.lower()
            if _is_query_mirror_name(name):
                continue
            if lower_object_names and name not in lower_object_names:
                continue
            objects_by_name[name] = DataObject(
                catalog=None,
                schema=pipeline_name,
                name=name,
                type=_view_type(connection._state, name),
            )

        pending_drops = connection._state.pending_drops()
        for object_name in pending_drops:
            objects_by_name.pop(object_name, None)

        for object_name in connection._state.pending_tables():
            if lower_object_names and object_name not in lower_object_names:
                continue
            objects_by_name[object_name] = DataObject(
                catalog=None,
                schema=pipeline_name,
                name=object_name,
                type=DataObjectType.TABLE,
            )

        for object_name in connection._state.pending_views():
            if lower_object_names and object_name not in lower_object_names:
                continue
            objects_by_name[object_name] = DataObject(
                catalog=None,
                schema=pipeline_name,
                name=object_name,
                type=_view_type(connection._state, object_name),
            )

        return list(objects_by_name.values())

    def columns(
        self, table_name: TableName, include_pseudo_columns: bool = False
    ) -> t.Dict[str, exp.DataType]:
        from feldera.pipeline import Pipeline

        connection = self.connection
        pipeline = Pipeline.get(connection._pipeline_name, connection._client)
        target = exp.to_table(table_name).name.lower()

        for obj in (*pipeline.tables(), *pipeline.views()):
            if obj.name.lower() == target:
                return {
                    field["name"]: _feldera_type_to_exp(
                        field.get("columntype", {}).get("type", "VARCHAR")
                        if isinstance(field.get("columntype"), dict)
                        else str(field.get("columntype", "VARCHAR"))
                    )
                    for field in (obj.fields or [])
                }

        raise SQLMeshError(
            f"Table/view '{target}' not found in pipeline '{connection._pipeline_name}'"
        )

    def get_current_catalog(self) -> t.Optional[str]:
        return None

    def create_view(
        self,
        view_name: TableName,
        query_or_df: t.Any,
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
        if replace:
            target_data_object = self.get_data_object(exp.to_table(view_name))
            if target_data_object is not None:
                self.drop_data_object(target_data_object, ignore_if_not_exists=True)

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
        if replace:
            return super()._create_table_from_source_queries(
                table_name,
                source_queries,
                target_columns_to_types=target_columns_to_types,
                exists=exists,
                replace=replace,
                table_description=table_description,
                column_descriptions=column_descriptions,
                table_kind=table_kind,
                track_rows_processed=track_rows_processed,
                **kwargs,
            )

        if not target_columns_to_types:
            raise SQLMeshError(
                "Feldera requires known column types when creating a table from a query."
            )

        with self.transaction(condition=len(source_queries) > 1):
            self._create_table_from_columns(
                table_name,
                target_columns_to_types,
                exists=exists,
                table_description=table_description,
                column_descriptions=column_descriptions,
                **kwargs,
            )
            for source_query in source_queries:
                with source_query as query:
                    self._insert_append_query(
                        table_name,
                        query,
                        target_columns_to_types,
                        track_rows_processed=track_rows_processed,
                    )

    def _insert_overwrite_by_condition(
        self,
        table_name: TableName,
        source_queries: t.List[SourceQuery],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        where: t.Optional[exp.Condition] = None,
        insert_overwrite_strategy_override: t.Optional[t.Any] = None,
        **kwargs: t.Any,
    ) -> None:
        # Whole-table replacement is cheaper in Feldera as DROP+CREATE than DELETE+INSERT.
        if where is None and source_queries:
            self.drop_table(table_name)
            self._create_table_from_source_queries(
                table_name,
                source_queries,
                target_columns_to_types=target_columns_to_types,
                exists=True,
                replace=False,
                **kwargs,
            )
            return

        super()._insert_overwrite_by_condition(
            table_name,
            source_queries,
            target_columns_to_types=target_columns_to_types,
            where=where,
            insert_overwrite_strategy_override=insert_overwrite_strategy_override,
            **kwargs,
        )

    def drop_table(self, table_name: TableName, exists: bool = True, **kwargs: t.Any) -> None:
        target_data_object = self.get_data_object(exp.to_table(table_name))
        if target_data_object:
            if target_data_object.type.is_materialized_view:
                self.drop_view(
                    table_name,
                    ignore_if_not_exists=exists,
                    materialized=True,
                    **kwargs,
                )
                return
            if target_data_object.type.is_view:
                self.drop_view(table_name, ignore_if_not_exists=exists, **kwargs)
                return
        super().drop_table(table_name, exists=exists, **kwargs)

    def create_schema(
        self,
        schema_name: SchemaName,
        ignore_if_exists: bool = True,
        warn_on_error: bool = True,
        properties: t.Optional[t.List[exp.Expr]] = None,
    ) -> None:
        return None

    def ping(self) -> None:
        self.connection._client.get_config()
