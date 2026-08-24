from __future__ import annotations

import logging
import re
import typing as t
from functools import cached_property

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sqlmesh.core.engine_adapter.base import EngineAdapter, _get_data_object_cache_key
from sqlmesh.core.engine_adapter.mixins import PandasNativeFetchDFSupportMixin
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
    SourceQuery,
    set_catalog,
)
from sqlmesh.core.dialect import to_schema
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter._typing import DF, Query

logger = logging.getLogger(__name__)


class Db2ErrorCodes:
    """Common Db2 SQL error codes used for exception inspection."""

    DUPLICATE_OBJECT = "SQL0601N"
    INDEX_EXISTS = "SQL0605W"


def is_db2_error(exception: Exception, error_code: str) -> bool:
    """Returns True when the exception message contains the given Db2 error code."""
    return error_code in str(exception)


@set_catalog()
class Db2EngineAdapter(
    PandasNativeFetchDFSupportMixin,
    EngineAdapter,
):
    DIALECT = "db2"
    SUPPORTS_INDEXES = True
    SUPPORTS_REPLACE_TABLE = False
    SUPPORTS_GRANTS = True
    COMMENT_CREATION_TABLE = CommentCreationTable.COMMENT_COMMAND_ONLY
    COMMENT_CREATION_VIEW = CommentCreationView.COMMENT_COMMAND_ONLY
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    # Db2 does not support DROP TABLE/VIEW ... CASCADE — doing so raises SQL0104N.
    # Schema cascade is handled manually inside drop_schema() and does not rely
    # on this flag, so the list is intentionally empty.
    # SUPPORTED_DROP_CASCADE_OBJECT_KINDS = ["SCHEMA", "TABLE", "VIEW"]
    SUPPORTED_DROP_CASCADE_OBJECT_KINDS: t.List[str] = []
    MAX_IDENTIFIER_LENGTH: t.Optional[int] = 128
    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            # DECIMAL without precision defaults to (5, 0)
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(5, 0), (0,)],
            # CHAR without length defaults to 1
            exp.DataType.build("CHAR", dialect=DIALECT).this: [(1,)],
            # VARCHAR without length defaults to 1
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: [(1,)],
            # TIMESTAMP defaults to 6 digits of fractional seconds
            exp.DataType.build("TIMESTAMP", dialect=DIALECT).this: [(6,)],
            # TIME defaults to 0 digits of fractional seconds
            exp.DataType.build("TIME", dialect=DIALECT).this: [(0,)],
        },
        "types_with_unlimited_length": {
            # CLOB can be used for unlimited text
            exp.DataType.build("CLOB", dialect=DIALECT).this: {
                exp.DataType.build("VARCHAR", dialect=DIALECT).this,
                exp.DataType.build("CHAR", dialect=DIALECT).this,
            },
        },
        "drop_cascade": False,
    }

    def get_current_catalog(self) -> t.Optional[str]:
        """
        Db2 requires FROM SYSIBM.SYSDUMMY1 to read the CURRENT SERVER special register.
        Returns uppercase to match the Db2 dialect's identifier normalisation.
        """
        result = self.fetchone("SELECT CURRENT SERVER FROM SYSIBM.SYSDUMMY1")
        if result:
            return result[0].upper() if result[0] else None
        return None

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
        Db2 requires every primary key column to carry an explicit NOT NULL constraint;
        the base class does not add this automatically.
        """
        expressions = expressions or []

        pk_columns = set()
        for expr in expressions:
            if isinstance(expr, exp.PrimaryKey):
                for col_expr in expr.expressions:
                    if isinstance(col_expr, exp.Column):
                        pk_columns.add(col_expr.name)

        column_defs = []
        for column, col_type in target_columns_to_types.items():
            col_def = self._build_column_def(
                column,
                column_descriptions=column_descriptions,
                engine_supports_schema_comments=(
                    self.COMMENT_CREATION_TABLE.supports_schema_def
                    if not is_view
                    else self.COMMENT_CREATION_VIEW.supports_schema_def
                ),
                col_type=None if is_view else col_type,
            )

            if column in pk_columns and not is_view:
                existing_constraints = col_def.args.get("constraints") or []
                has_not_null = any(
                    isinstance(c, exp.NotNullColumnConstraint) for c in existing_constraints
                )
                if not has_not_null:
                    existing_constraints.append(exp.NotNullColumnConstraint())
                    col_def.set("constraints", existing_constraints)

            column_defs.append(col_def)

        return exp.Schema(
            this=table,
            expressions=column_defs + expressions,
        )

    def create_index(
        self,
        table_name: TableName,
        index_name: str,
        columns: t.Tuple[str, ...],
        exists: bool = True,
    ) -> None:
        """
        Db2 does not support CREATE INDEX IF NOT EXISTS, so we query SYSCAT.INDEXES
        first and skip creation when the index already exists.  SQL0605W (index
        already defined) is caught as a fallback for any race between the check
        and the create.
        """
        if not self.SUPPORTS_INDEXES:
            return

        table = exp.to_table(table_name)
        schema_name = table.db or self._get_current_schema()

        self.execute(
            exp.select(exp.column("INDNAME"))
            .from_("SYSCAT.INDEXES")
            .where(
                exp.and_(
                    exp.func("UPPER", exp.column("TABSCHEMA")).eq(
                        exp.Literal.string(schema_name.upper())
                    ),
                    exp.func("UPPER", exp.column("TABNAME")).eq(
                        exp.Literal.string(table.alias_or_name.upper())
                    ),
                    exp.func("UPPER", exp.column("INDNAME")).eq(
                        exp.Literal.string(index_name.upper())
                    ),
                )
            )
        )
        if self.cursor.fetchone():
            logger.debug("Index %s already exists on %s, skipping", index_name, table_name)
            return

        expression = exp.Create(
            this=exp.Index(
                this=exp.to_identifier(index_name),
                table=exp.to_table(table_name),
                params=exp.IndexParameters(columns=[exp.to_column(c) for c in columns]),
            ),
            kind="INDEX",
            exists=False,
        )

        try:
            self.execute(expression)
        except Exception as e:
            # DB2 can return either SQL0605W (index exists warning) or
            # SQL0601N (duplicate object name error) when index already exists
            if is_db2_error(e, Db2ErrorCodes.INDEX_EXISTS) or is_db2_error(
                e, Db2ErrorCodes.DUPLICATE_OBJECT
            ):
                logger.debug("Index %s already exists, skipping", index_name)
                return
            raise

    def columns(
        self, table_name: TableName, include_pseudo_columns: bool = False
    ) -> t.Dict[str, exp.DataType]:
        """
        Reads column metadata from SYSCAT.COLUMNS.  When no rows are returned for
        an exact name match, a prefix query is attempted because Db2 truncates
        identifiers that exceed MAX_IDENTIFIER_LENGTH.
        """
        table = exp.to_table(table_name)
        schema_name = table.db or self._get_current_schema()
        table_name_str = table.alias_or_name

        self.execute(
            exp.select(
                exp.column("COLNAME").as_("column_name"),
                exp.column("TYPENAME").as_("data_type"),
                exp.column("LENGTH").as_("length"),
                exp.column("SCALE").as_("scale"),
            )
            .from_("SYSCAT.COLUMNS")
            .where(
                exp.and_(
                    exp.func("UPPER", exp.column("TABSCHEMA")).eq(
                        exp.Literal.string(schema_name.upper())
                    ),
                    exp.func("UPPER", exp.column("TABNAME")).eq(
                        exp.Literal.string(table_name_str.upper())
                    ),
                )
            )
            .order_by("COLNO")
        )
        resp = self.cursor.fetchall()

        if not resp:
            # Db2 may have stored a truncated version of the name; try a prefix match.
            prefix = table_name_str[:100]
            logger.debug(
                "Exact column lookup failed for %s.%s; retrying with prefix %s%%",
                schema_name,
                table_name_str,
                prefix,
            )
            self.execute(
                exp.select(
                    exp.column("TABNAME"),
                    exp.column("COLNAME").as_("column_name"),
                    exp.column("TYPENAME").as_("data_type"),
                    exp.column("LENGTH").as_("length"),
                    exp.column("SCALE").as_("scale"),
                )
                .from_("SYSCAT.COLUMNS")
                .where(
                    exp.and_(
                        exp.func("UPPER", exp.column("TABSCHEMA")).eq(
                            exp.Literal.string(schema_name.upper())
                        ),
                        exp.column("TABNAME").like(exp.Literal.string(f"{prefix.upper()}%")),
                    )
                )
                .order_by("TABNAME", "COLNO")
            )
            prefix_resp = self.cursor.fetchall()

            if not prefix_resp:
                raise SQLMeshError(
                    f"Could not get columns for table '{table.sql(dialect=self.dialect)}'. "
                    f"Table not found in SYSCAT.COLUMNS (tried exact match and prefix '{prefix}%')."
                )

            actual_table_name = prefix_resp[0][0]
            logger.debug(
                "Resolved %s.%s via prefix to %s.%s",
                schema_name,
                table_name_str,
                schema_name,
                actual_table_name,
            )
            resp = [(row[1], row[2], row[3], row[4]) for row in prefix_resp]

        return {
            column_name: self._db2_type_to_sqlglot(data_type, length, scale)
            for column_name, data_type, length, scale in resp
        }

    def _db2_type_to_sqlglot(self, db2_type: str, length: int, scale: int) -> exp.DataType:
        """Maps a Db2 catalog type name to a sqlglot DataType, using length and scale where applicable."""
        db2_type = db2_type.upper()
        type_mapping = {
            "INTEGER": "INT",
            "INT": "INT",
            "BIGINT": "BIGINT",
            "SMALLINT": "SMALLINT",
            "DOUBLE": "DOUBLE",
            "REAL": "REAL",
            "FLOAT": "DOUBLE",
            "DECIMAL": f"DECIMAL({length},{scale})",
            "NUMERIC": f"DECIMAL({length},{scale})",
            "DECFLOAT": "DOUBLE",
            "VARCHAR": f"VARCHAR({length})",
            "CHAR": f"CHAR({length})",
            "CHARACTER": f"CHAR({length})",
            "CLOB": "CLOB",
            "GRAPHIC": f"CHAR({length})",
            "VARGRAPHIC": f"VARCHAR({length})",
            "DBCLOB": "CLOB",
            "DATE": "DATE",
            "TIMESTAMP": "TIMESTAMP",
            "TIME": "TIME",
            "BLOB": "BLOB",
            "BINARY": f"BINARY({length})",
            "VARBINARY": f"VARBINARY({length})",
            "XML": "TEXT",
            "ROWID": "VARCHAR(40)",
            "BOOLEAN": "BOOLEAN",
        }
        sqlglot_type = type_mapping.get(db2_type, f"VARCHAR({length})")
        return exp.DataType.build(sqlglot_type, dialect="db2")

    def _get_current_grants_config(self, table: exp.Table) -> t.Dict[str, t.List[str]]:
        """
        Db2 does not have INFORMATION_SCHEMA.TABLE_PRIVILEGES.
        Query SYSCAT.TABAUTH which stores per-privilege columns (SELECTAUTH, INSERTAUTH,
        etc.) with values 'Y' (granted) or 'G' (granted with grant option).
        Filter by GRANTOR = CURRENT USER to return only grants made by the connected user.
        """
        schema_name = (table.args.get("db") or self._get_current_schema()).upper()  # type: ignore
        table_name = table.name.upper()

        rows = self.fetchall(
            exp.select(
                exp.column("GRANTEE"),
                exp.column("SELECTAUTH"),
                exp.column("INSERTAUTH"),
                exp.column("UPDATEAUTH"),
                exp.column("DELETEAUTH"),
                exp.column("ALTERAUTH"),
                exp.column("INDEXAUTH"),
                exp.column("CONTROLAUTH"),
            )
            .from_("SYSCAT.TABAUTH")
            .where(
                exp.and_(
                    exp.func("UPPER", exp.column("TABSCHEMA")).eq(
                        exp.Literal.string(schema_name)
                    ),
                    exp.func("UPPER", exp.column("TABNAME")).eq(
                        exp.Literal.string(table_name)
                    ),
                    exp.column("GRANTOR").eq(
                        exp.func("UPPER", exp.Anonymous(this="CURRENT USER", expressions=[]))
                    ),
                    exp.column("GRANTEE").neq(
                        exp.func("UPPER", exp.Anonymous(this="CURRENT USER", expressions=[]))
                    ),
                )
            )
        )

        # SYSCAT column name → SQL privilege name
        col_to_priv = {
            "SELECTAUTH": "SELECT",
            "INSERTAUTH": "INSERT",
            "UPDATEAUTH": "UPDATE",
            "DELETEAUTH": "DELETE",
            "ALTERAUTH": "ALTER",
            "INDEXAUTH": "INDEX",
            "CONTROLAUTH": "CONTROL",
        }
        grants: t.Dict[str, t.List[str]] = {}
        for row in rows:
            grantee = str(row[0]).strip()
            for i, (_, priv) in enumerate(col_to_priv.items(), start=1):
                val = str(row[i]).strip() if row[i] is not None else "N"
                if val in ("Y", "G"):
                    grants.setdefault(priv, [])
                    if grantee not in grants[priv]:
                        grants[priv].append(grantee)
        return grants

    def _apply_grants_config_expr(
        self,
        table: exp.Table,
        grants_config: t.Dict[str, t.List[str]],
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> t.List[exp.Expr]:
        """Generate GRANT statements for Db2."""
        exprs: t.List[exp.Expr] = []
        for privilege, principals in grants_config.items():
            for principal in principals:
                exprs.append(
                    exp.Grant(
                        privileges=[exp.GrantPrivilege(this=exp.Var(this=privilege))],
                        kind=exp.Var(this="TABLE"),
                        securable=table.copy(),
                        principals=[
                            exp.GrantPrincipal(this=exp.Var(this=principal))
                        ],
                    )
                )
        return exprs

    def _revoke_grants_config_expr(
        self,
        table: exp.Table,
        grants_config: t.Dict[str, t.List[str]],
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> t.List[exp.Expr]:
        """Generate REVOKE statements for Db2."""
        exprs: t.List[exp.Expr] = []
        for privilege, principals in grants_config.items():
            for principal in principals:
                exprs.append(
                    exp.Revoke(
                        privileges=[exp.GrantPrivilege(this=exp.Var(this=privilege))],
                        kind=exp.Var(this="TABLE"),
                        securable=table.copy(),
                        principals=[
                            exp.GrantPrincipal(this=exp.Var(this=principal))
                        ],
                    )
                )
        return exprs

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.SINGLE_CATALOG_ONLY

    def table_exists(self, table_name: TableName) -> bool:
        """
        Db2 doesn't support DESCRIBE so we query SYSCAT.TABLES directly.
        UPPER() is used for case-insensitive comparison since Db2 stores unquoted
        identifiers in uppercase but callers may pass lowercase names.
        """
        table = exp.to_table(table_name)
        data_object_cache_key = _get_data_object_cache_key(table.catalog, table.db, table.name)
        if data_object_cache_key in self._data_object_cache:
            logger.debug("Table existence cache hit: %s", data_object_cache_key)
            return self._data_object_cache[data_object_cache_key] is not None

        schema_name = table.db or self._get_current_schema()
        table_name_str = table.alias_or_name

        self.execute(
            exp.select(
                exp.column("TABSCHEMA"),
                exp.column("TABNAME"),
            )
            .from_("SYSCAT.TABLES")
            .where(
                exp.and_(
                    exp.func("UPPER", exp.column("TABSCHEMA")).eq(
                        exp.Literal.string(schema_name.upper())
                    ),
                    exp.func("UPPER", exp.column("TABNAME")).eq(
                        exp.Literal.string(table_name_str.upper())
                    ),
                )
            )
        )
        result = self.cursor.fetchone()

        if result is not None:
            actual_schema, actual_table = result
            self._data_object_cache[data_object_cache_key] = DataObject(
                name=actual_table,
                schema=actual_schema,
                type=DataObjectType.TABLE,
            )

        return result is not None

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
        """
        Db2 doesn't support IF NOT EXISTS in CREATE TABLE, so we always pass
        exists=False and handle the existence check in _create_table instead.
        """
        return super()._build_create_table_exp(
            table_name_or_schema=table_name_or_schema,
            expression=expression,
            exists=False,
            replace=replace,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            table_kind=table_kind,
            **kwargs,
        )

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
        """
        Db2 doesn't support IF NOT EXISTS or CREATE OR REPLACE TABLE, so existence
        is checked explicitly. For CTAS, Db2 requires WITH DATA and rejects the
        _subquery alias the base class injects — both fixed in SQL after generation.
        """
        table_name = (
            table_name_or_schema.this
            if isinstance(table_name_or_schema, exp.Schema)
            else table_name_or_schema
        )
        table = exp.to_table(table_name)

        if expression and isinstance(expression, (exp.Select, exp.Subquery)):
            # Check table exists — also drop any view left with the same name
            # (a previous failed run may have left a staging view in place).
            if self.table_exists(table):
                if exists and not replace:
                    return
                self.drop_table(table)
            else:
                self.drop_view(table, ignore_if_not_exists=True)

            create_exp = self._build_create_table_exp(
                table_name_or_schema=table_name_or_schema,
                expression=expression,
                exists=False,
                replace=False,
                target_columns_to_types=target_columns_to_types,
                table_description=table_description,
                table_kind=table_kind,
                **kwargs,
            )
            sql = self._to_sql(create_exp)

            # Db2 requires WITH DATA after the AS clause in CTAS, with the entire
            # source query wrapped in parentheses. The Db2 dialect generates
            # _subquery unquoted; the old quoted pattern never matched but the
            # wrapping below handles it correctly regardless.
            if "WITH DATA" not in sql.upper() and "WITH NO DATA" not in sql.upper():
                match = re.search(r"CREATE\s+TABLE\s+\S+\s+AS\s+", sql, re.IGNORECASE)
                if match:
                    pos = match.end()
                    sql = sql[:pos] + "(" + sql[pos:].rstrip(";").rstrip() + ") WITH DATA"
                else:
                    sql = sql.rstrip(";").rstrip() + " WITH DATA"

            self.execute(sql, track_rows_processed=track_rows_processed)

            if self.comments_enabled:
                if table_description and self.COMMENT_CREATION_TABLE.is_comment_command_only:
                    self._create_table_comment(table_name, table_description)
                if column_descriptions:
                    self._create_column_comments(table_name, column_descriptions)
        else:
            # Non-CTAS path: guard existence manually since Db2 lacks IF NOT EXISTS.
            if exists and self.table_exists(table):
                return
            super()._create_table(
                table_name_or_schema=table_name_or_schema,
                expression=expression,
                exists=False,
                replace=replace,
                target_columns_to_types=target_columns_to_types,
                table_description=table_description,
                column_descriptions=column_descriptions,
                table_kind=table_kind,
                track_rows_processed=track_rows_processed,
                **kwargs,
            )

    def drop_view(
        self,
        view_name: TableName,
        ignore_if_not_exists: bool = True,
        materialized: bool = False,
        **kwargs: t.Any,
    ) -> None:
        """
        Db2 doesn't support DROP VIEW IF EXISTS, so existence is checked via
        SYSCAT.VIEWS before issuing a plain DROP VIEW. UPPER() is used for
        case-insensitive comparison, consistent with table_exists.
        """
        table = exp.to_table(view_name)
        schema_name = table.db or self._get_current_schema()

        self.execute(
            exp.select("1")
            .from_("SYSCAT.VIEWS")
            .where(
                exp.and_(
                    exp.func("UPPER", exp.column("VIEWSCHEMA")).eq(
                        exp.Literal.string(schema_name.upper())
                    ),
                    exp.func("UPPER", exp.column("VIEWNAME")).eq(
                        exp.Literal.string(table.name.upper())
                    ),
                )
            )
        )
        if not self.cursor.fetchone():
            if ignore_if_not_exists:
                return
            raise SQLMeshError(f"View '{table.sql(dialect=self.dialect)}' does not exist.")

        self.execute(exp.Drop(this=table, kind="VIEW", exists=False))
        self._clear_data_object_cache(view_name)

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Queries SYSCAT.TABLES for all tables and views in the given schema.
        ibm_db returns column names in uppercase regardless of SQL aliases, so
        the DataFrame columns are normalised to lowercase before iteration.
        """
        catalog = self.get_current_catalog()
        schema = to_schema(schema_name).db

        query = (
            exp.select(
                exp.column("TABNAME").as_("name"),
                exp.column("TABSCHEMA").as_("schema_name"),
                exp.case()
                .when(exp.column("TYPE").eq("T"), exp.Literal.string("table"))
                .when(exp.column("TYPE").eq("V"), exp.Literal.string("view"))
                .else_(exp.column("TYPE"))
                .as_("type"),
            )
            .from_(exp.table_("TABLES", db="SYSCAT"))
            .where(
                exp.func("UPPER", exp.column("TABSCHEMA")).eq(exp.Literal.string(schema.upper()))
            )
        )

        if object_names:
            query = query.where(
                exp.func("UPPER", exp.column("TABNAME")).isin(*[n.upper() for n in object_names])
            )

        df = self.fetchdf(query)
        df.columns = [c.lower() for c in df.columns]  # type: ignore

        return [
            DataObject(
                catalog=catalog,
                schema=row.schema_name,  # type: ignore
                name=row.name,  # type: ignore
                type=DataObjectType.from_str(row.type),  # type: ignore
            )
            for row in df.itertuples()
        ]

    def _get_current_schema(self) -> str:
        """
        Returns the active schema for the connection.

        CURRENT SCHEMA defaults to the connected username in Db2, but can be set
        to an empty string via SET CURRENT SCHEMA = ''.  If it is empty, fall back
        to CURRENT USER (the authorization name, which always equals the default
        schema Db2 would create on first connect).
        """
        result = self.fetchone("SELECT CURRENT SCHEMA FROM SYSIBM.SYSDUMMY1")
        if result and result[0] and result[0].strip():
            return result[0].lower()
        user = self.fetchone("SELECT CURRENT USER FROM SYSIBM.SYSDUMMY1")
        if user and user[0] and user[0].strip():
            return user[0].lower()
        raise SQLMeshError(
            "Could not determine the current Db2 schema. "
            "CURRENT SCHEMA and CURRENT USER are both empty. "
            "Set the db2_schema connection option explicitly."
        )

    def create_schema(
        self,
        schema_name: SchemaName,
        ignore_if_exists: bool = True,
        warn_on_error: bool = True,
        properties: t.Optional[t.List[exp.Expression]] = None,
        **kwargs: t.Any,
    ) -> None:
        """
        Db2 has no CREATE SCHEMA IF NOT EXISTS, so SYSCAT.SCHEMATA is queried first.
        SQL0601N (duplicate object) is caught as a fallback for any race between the
        check and the create.
        """
        schema = to_schema(schema_name)
        schema_name_str = schema.db

        if ignore_if_exists:
            self.execute(
                exp.select("1")
                .from_("SYSCAT.SCHEMATA")
                .where(
                    exp.func("UPPER", exp.column("SCHEMANAME")).eq(
                        exp.Literal.string(schema_name_str.upper())
                    )
                )
            )
            if self.cursor.fetchone():
                logger.debug("Schema %s already exists", schema_name_str)
                return

        try:
            self.execute(
                exp.Create(
                    this=exp.Schema(this=exp.to_identifier(schema_name_str)),
                    kind="SCHEMA",
                )
            )
        except Exception as e:
            if ignore_if_exists and is_db2_error(e, Db2ErrorCodes.DUPLICATE_OBJECT):
                logger.debug("Schema %s already exists (SQL0601N)", schema_name_str)
                return
            raise

    def drop_schema(
        self,
        schema_name: SchemaName,
        ignore_if_not_exists: bool = True,
        cascade: bool = False,
        **kwargs: t.Any,
    ) -> None:
        """
        Db2 only supports DROP SCHEMA … RESTRICT (never CASCADE), so when cascade=True
        all views are dropped before tables — views first because they may depend on
        tables and would block the table drop otherwise.
        """
        schema = to_schema(schema_name)
        schema_name_str = schema.db.upper()

        if ignore_if_not_exists:
            self.execute(
                exp.select("1")
                .from_("SYSCAT.SCHEMATA")
                .where(exp.column("SCHEMANAME").eq(exp.Literal.string(schema_name_str)))
            )
            if not self.cursor.fetchone():
                logger.debug("Schema %s does not exist, skipping drop", schema_name_str)
                return

        if cascade:
            # Views must be dropped before tables; a view depending on a table would
            # otherwise cause the table drop to fail with SQL0478N.
            for kind, type_code in (("VIEW", "V"), ("TABLE", "T")):
                self.execute(
                    exp.select("TABNAME")
                    .from_("SYSCAT.TABLES")
                    .where(
                        exp.and_(
                            exp.column("TABSCHEMA").eq(exp.Literal.string(schema_name_str)),
                            exp.column("TYPE").eq(exp.Literal.string(type_code)),
                        )
                    )
                )
                for (obj_name,) in self.cursor.fetchall():
                    self.execute(
                        exp.Drop(
                            this=exp.to_table(f"{schema_name_str}.{obj_name}"),
                            kind=kind,
                        )
                    )

        # Db2 requires RESTRICT — use raw SQL since sqlglot does not emit it for schemas.
        self.execute(f"DROP SCHEMA {schema_name_str} RESTRICT")

    def _merge(
        self,
        target_table: TableName,
        query: Query,
        on: exp.Expr,
        whens: exp.Whens,
    ) -> None:
        """
        Db2 rejects double-underscore aliases such as __MERGE_TARGET__, so the
        base-class placeholder aliases are replaced with TARGET and SOURCE before
        the MERGE statement is executed.
        """
        this = exp.alias_(exp.to_table(target_table), alias="TARGET", table=True)
        using = exp.alias_(exp.Subquery(this=query), alias="SOURCE", copy=False, table=True)

        def _replace_alias(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column):
                if node.table == "__MERGE_TARGET__":
                    return exp.column(node.name, table="TARGET")
                if node.table == "__MERGE_SOURCE__":
                    return exp.column(node.name, table="SOURCE")
            return node

        self.execute(
            exp.Merge(
                this=this,
                using=using,
                on=on.transform(_replace_alias),
                whens=whens.transform(_replace_alias),
            ),
            track_rows_processed=True,
        )

    def _create_table_like(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        exists: bool,
        **kwargs: t.Any,
    ) -> None:
        self.execute(
            exp.Create(
                this=exp.Schema(
                    this=exp.to_table(target_table_name),
                    expressions=[exp.LikeProperty(this=exp.to_table(source_table_name))],
                ),
                kind="TABLE",
                # Always pass exists=False here: Db2 pre-11.5.8 does not support
                # IF NOT EXISTS, and the rest of the adapter guards existence
                # explicitly via _create_table rather than relying on the dialect.
                # The caller is responsible for the existence check before reaching
                # this point, consistent with _build_create_table_exp.
                exists=False,
            )
        )

    def _truncate_table(self, table_name: TableName) -> None:
        # Db2's TRUNCATE TABLE ... IMMEDIATE requires being the first statement
        # in a unit of work (SQL0428N). ibm_db_dbi forces AUTOCOMMIT_OFF on all
        # connections, so _prepare_helper() inside execute() implicitly opens a
        # unit of work before TRUNCATE runs — making it impossible to satisfy
        # that constraint. DELETE FROM has no such restriction and is
        # rollback-safe, matching the pattern used by trino.py and risingwave.py.
        self.execute(exp.Delete(this=exp.to_table(table_name)))

    def _convert_df_datetime(self, df: DF, columns_to_types: t.Dict[str, exp.DataType]) -> None:
        """
        Db2 has strict type casting rules: TIME columns cannot be cast to TIMESTAMP or
        DATE, so datetime-typed pandas columns are converted to strings before insert.
        """
        import pandas as pd
        from pandas.api.types import is_datetime64_any_dtype  # type: ignore

        for column, kind in columns_to_types.items():
            if column not in df.columns:
                continue

            if kind.is_type(exp.DataType.Type.TIME):  # type: ignore
                if is_datetime64_any_dtype(df.dtypes[column]):  # type: ignore
                    df[column] = pd.to_datetime(df[column]).dt.strftime("%H:%M:%S")  # type: ignore
                else:
                    df[column] = df[column].astype(str)  # type: ignore
            elif kind.is_type(exp.DataType.Type.DATE):  # type: ignore
                df[column] = pd.to_datetime(df[column]).dt.strftime("%Y-%m-%d")  # type: ignore
            elif is_datetime64_any_dtype(df.dtypes[column]):  # type: ignore
                df[column] = pd.to_datetime(df[column]).dt.strftime("%Y-%m-%d %H:%M:%S")  # type: ignore

    def _fetch_native_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> "DF":
        """
        Db2 stores identifiers created with quoting as case-sensitive (e.g. "id").
        The base class and the snapshot evaluator both call _fetch_native_df with
        quote_identifiers=False, which leaves column references unquoted.  Db2
        uppercases unquoted identifiers at parse time, so SELECT id FROM tbl
        becomes a lookup for ID — causing SQL0206N against a table whose columns
        were stored as case-sensitive lowercase "id" by CREATE TABLE.

        Forcing quote_identifiers=True here ensures every SELECT issued by
        SQLMesh (evaluator, fetchdf, fetchall via execute) wraps identifiers in
        double-quotes so Db2 matches them exactly as stored.

        normalize_identifiers is applied first so that unquoted identifiers are
        uppercased before quoting (e.g. unquoted `c` → `C` → `"C"`).  Quoted
        identifiers (e.g. `"a"`, `"B"`) are intentionally left unchanged by
        normalize_identifiers — they remain case-sensitive as the caller intended.
        This prevents the mismatch where a CTE alias defined as unquoted `c` would
        otherwise be emitted as `"c"` (lowercase) while a SELECT reference derived
        from Db2's UPPERCASE normalisation strategy uses `"C"` (SQL0204N).
        """
        if isinstance(query, exp.Expression):
            query = query.copy()
            # The db2_sqlglot generator injects FROM SYSIBM.SYSDUMMY1 via a
            # preprocessor registered on exp.Select.  When the caller passes
            # Alias(Select, alias=name) — e.g. exp.select(expr).as_("col") —
            # the generator renders the inner Select (adding FROM SYSDUMMY1)
            # and then appends AS name after the fully-rendered SQL, producing:
            #   SELECT ... FROM SYSIBM.SYSDUMMY1 AS name   ← broken
            # instead of:
            #   SELECT ... AS name FROM SYSIBM.SYSDUMMY1   ← correct
            # This is a db2_sqlglot dialect bug (the Alias wrapper is not
            # SELECT-aware).  Work around it: when the top-level node is
            # Alias(Select), move the alias onto the first selected expression
            # so the generator only ever sees a bare Select node.
            if isinstance(query, exp.Alias) and isinstance(query.this, exp.Select):
                inner = query.this
                alias_name = query.alias
                inner.set(
                    "expressions",
                    [exp.Alias(this=inner.expressions[0], alias=exp.to_identifier(alias_name))]
                    + inner.expressions[1:],
                )
                query = inner
            normalize_identifiers(query, dialect=self.dialect)
        return super()._fetch_native_df(query, quote_identifiers=True)

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        """Converts datetime columns to strings before delegating to the base implementation."""
        from sqlmesh.core.dialect import get_source_columns_to_types

        source_columns_to_types = get_source_columns_to_types(
            target_columns_to_types, source_columns
        )
        self._convert_df_datetime(df, source_columns_to_types)

        return super()._df_to_source_queries(
            df, target_columns_to_types, batch_size, target_table, source_columns
        )

    def set_current_catalog(self, catalog: str) -> None:
        """Switches the active catalog using Db2's CONNECT TO statement."""
        self.execute(f"CONNECT TO {catalog}")
        logger.debug("Switched to catalog: %s", catalog)

    @cached_property
    def server_version(self) -> t.Tuple[int, int]:
        """Lazily fetch and cache major and minor Db2 server version."""
        if result := self.fetchone("SELECT SERVICE_LEVEL FROM SYSIBMADM.ENV_INST_INFO"):
            version_str = result[0]
            match = re.search(r"v?(\d+)\.(\d+)", version_str)
            if match:
                return int(match.group(1)), int(match.group(2))
        return 11, 5  # Default to Db2 11.5
