import json

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect, DialectType

dialects = json.dumps(
    [
        {
            "dialect_title": "SQLGlot" if dialect == "" else dialect_class.__name__,
            "dialect_name": dialect,
        }
        for dialect, dialect_class in Dialect.classes.items()
    ]
)


def parse_to_json(sql: str, read: DialectType = None) -> str:
    return json.dumps(
        [
            exp.dump() if exp else {}
            for exp in sqlglot.parse(sql, read=read, error_level=sqlglot.ErrorLevel.IGNORE)
        ]
    )


def get_dialect(name: str = "") -> str:
    dialect = Dialect.classes.get(name, Dialect)
    keywords = dialect.tokenizer_class.KEYWORDS
    type_mapping = dialect.generator_class.TYPE_MAPPING

    return json.dumps(
        {
            "keywords": " ".join(keywords),
            "types": " ".join(type_mapping.get(t, t.value) for t in exp.DataType.Type),
        }
    )


def format(sql: str = "", read: DialectType = None) -> str:
    return "\n".join(
        sqlglot.transpile(sql, read=read, error_level=sqlglot.errors.ErrorLevel.IGNORE, pretty=True)
    )


def validate(sql: str = "", read: DialectType = None) -> str:
    try:
        sqlglot.transpile(
            sql, read=read, pretty=False, unsupported_level=sqlglot.errors.ErrorLevel.IMMEDIATE
        )
    except sqlglot.errors.ParseError:
        return json.dumps(False)

    return json.dumps(True)


{
    "dialects": dialects,
    "get_dialect": get_dialect,
    "parse_to_json": parse_to_json,
    "validate": validate,
    "format": format,
}
