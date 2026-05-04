from __future__ import annotations

import typing as t

from sqlglot import exp
from sqlglot.generator import Generator
from sqlglot.dialects.dialect import Dialect

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.engine_adapter.shared import (
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
)
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    import pandas as pd

    from sqlmesh.core._typing import SchemaName, TableName


class FelderaDialect(Dialect):
    class Generator(Generator):
        TYPE_MAPPING = {
            **Generator.TYPE_MAPPING,
            exp.DataType.Type.FLOAT: "REAL",
            exp.DataType.Type.INT: "INTEGER",
        }
        TRANSFORMS = {
            **Generator.TRANSFORMS,
            exp.DateStrToDate: lambda self, expression: (
                f"CAST({self.sql(expression, 'this')} AS DATE)"
            ),
        }


_FELDERA_TO_EXP_TYPE: t.Dict[str, exp.DataType.Type] = {
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


class FelderaEngineAdapter(EngineAdapter):
    DIALECT = "felderadialect"
    SUPPORTS_TRANSACTIONS = False
    SUPPORTS_INDEXES = False
    SUPPORTS_MATERIALIZED_VIEWS = True
    SUPPORTS_REPLACE_TABLE = False
    COMMENT_CREATION_TABLE = CommentCreationTable.UNSUPPORTED
    COMMENT_CREATION_VIEW = CommentCreationView.UNSUPPORTED

    def _fetch_native_df(
        self,
        query: t.Union[exp.Expression, str],
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
        lower_object_names = (
            {name.lower() for name in object_names} if object_names else None
        )

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
            if lower_object_names and name not in lower_object_names:
                continue
            objects_by_name[name] = DataObject(
                catalog=None,
                schema=pipeline_name,
                name=name,
                type=DataObjectType.MATERIALIZED_VIEW,
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
                type=DataObjectType.MATERIALIZED_VIEW,
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
            "Table/view "
            f"'{target}' not found in pipeline '{connection._pipeline_name}'"
        )

    def get_current_catalog(self) -> t.Optional[str]:
        return None

    def _replace_materialized_view(
        self,
        table_name: TableName,
        query_or_df: t.Any,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        target_table = exp.to_table(table_name)
        target_data_object = self.get_data_object(target_table)

        if target_data_object is not None:
            self.drop_data_object(target_data_object, ignore_if_not_exists=True)

        self.create_view(
            target_table,
            query_or_df,
            target_columns_to_types=target_columns_to_types,
            replace=False,
            materialized=True,
            table_description=table_description,
            column_descriptions=column_descriptions,
            source_columns=source_columns,
            **kwargs,
        )

    def replace_query(
        self,
        table_name: TableName,
        query_or_df: t.Any,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        supports_replace_table_override: t.Optional[bool] = None,
        **kwargs: t.Any,
    ) -> None:
        self._replace_materialized_view(
            table_name,
            query_or_df,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            column_descriptions=column_descriptions,
            source_columns=source_columns,
            **kwargs,
        )

    def insert_overwrite_by_time_partition(
        self,
        table_name: TableName,
        query_or_df: t.Any,
        start: t.Any,
        end: t.Any,
        time_formatter: t.Any,
        time_column: t.Any,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        self._replace_materialized_view(
            table_name,
            query_or_df,
            target_columns_to_types=target_columns_to_types,
            source_columns=source_columns,
            **kwargs,
        )

    def drop_table(self, table_name: TableName, exists: bool = True, **kwargs: t.Any) -> None:
        target_data_object = self.get_data_object(exp.to_table(table_name))
        if target_data_object and target_data_object.type.is_view:
            self.drop_view(table_name, exists=exists, **kwargs)
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
