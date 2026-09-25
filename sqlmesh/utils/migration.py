from sqlglot.dialects.dialect import DialectType

# Sizes based on a composite key/index of two text fields with 4 bytes per characters.
MAX_TEXT_INDEX_LENGTH = {
    "mysql": "250",  # 250 characters per column, <= 767 byte index size limit
    "tsql": "450",  # 450 bytes per column, <= 900 byte index size limit
    "db2": "255",  # Db2 has strict primary key size limits, keep it conservative
}


def index_text_type(dialect: DialectType) -> str:
    """
    MySQL and MSSQL cannot create indexes or primary keys on TEXT fields; they
    require that the fields have a VARCHAR type of fixed length.

    This helper abstracts away the type of such fields.
    """

    return (
        f"VARCHAR({MAX_TEXT_INDEX_LENGTH[str(dialect)]})"
        if dialect in MAX_TEXT_INDEX_LENGTH
        else "TEXT"
    )


def blob_text_type(dialect: DialectType) -> str:
    if dialect == "mysql":
        return "LONGTEXT"
    if dialect == "db2":
        return "VARCHAR(32000)"
    return "TEXT"
