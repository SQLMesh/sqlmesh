from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.generator import Generator as SQLGlotGenerator


class SQLMeshFelderaDialect(Dialect):
    class Generator(SQLGlotGenerator):
        TYPE_MAPPING = {
            **SQLGlotGenerator.TYPE_MAPPING,
            exp.DataType.Type.FLOAT: "REAL",
            exp.DataType.Type.INT: "INTEGER",
        }
        TRANSFORMS = {
            **SQLGlotGenerator.TRANSFORMS,
            exp.CurrentTimestamp: lambda self, expression: (
                self.func("CURRENT_TIMESTAMP", expression.this)
                if expression.this
                else "CURRENT_TIMESTAMP"
            ),
            exp.DateStrToDate: lambda self, expression: (
                f"CAST({self.sql(expression, 'this')} AS DATE)"
            ),
        }


def register_feldera_dialect() -> None:
    if Dialect.get("feldera") is None:
        Dialect.classes["feldera"] = SQLMeshFelderaDialect


register_feldera_dialect()