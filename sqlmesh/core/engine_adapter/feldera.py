from __future__ import annotations

import typing as t

from sqlglot import exp
from sqlglot.dialects import generator
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
    class Generator(generator.Generator):
        TYPE_MAPPING = {
            **generator.Generator.TYPE_MAPPING,
            exp.DataType.Type.FLOAT: "REAL",
            exp.DataType.Type.INT: "INTEGER",
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

        objects: t.List[DataObject] = []
        for table in pipeline.tables():
            name = table.name.lower()
            if lower_object_names and name not in lower_object_names:
                continue
            objects.append(
                DataObject(
                    catalog=None,
                    schema=pipeline_name,
                    name=name,
                    type=DataObjectType.TABLE,
                )
            )

        for view in pipeline.views():
            name = view.name.lower()
            if lower_object_names and name not in lower_object_names:
                continue
            objects.append(
                DataObject(
                    catalog=None,
                    schema=pipeline_name,
                    name=name,
                    type=DataObjectType.MATERIALIZED_VIEW,
                )
            )

        return objects

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

    def ping(self) -> None:
        self.connection._client.get_config()
