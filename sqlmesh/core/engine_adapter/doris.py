# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections import defaultdict
import datetime
from decimal import Decimal
import hashlib
import itertools
import logging
import re
import threading
import time
import typing as t

import pandas as pd  # noqa: TID253
from sqlglot import exp, parse, parse_one
from sqlglot.helper import ensure_list
from sqlglot.optimizer.qualify_columns import quote_identifiers

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.base import (
    EngineAdapterWithIndexSupport,
    InsertOverwriteStrategy,
    MERGE_SOURCE_ALIAS,
    MERGE_TARGET_ALIAS,
    _decoded_str,
)
from sqlmesh.core.engine_adapter.mixins import (
    GetCurrentCatalogFromFunctionMixin,
    PandasNativeFetchDFSupportMixin,
    RowDiffMixin,
)
from sqlmesh.core.engine_adapter.shared import (
    CatalogSupport,
    CommentCreationTable,
    CommentCreationView,
    DataObject,
    DataObjectType,
    SourceQuery,
    set_catalog,
)
from sqlmesh.utils import columns_to_types_all_known, get_source_columns_to_types, random_id
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter._typing import DF, QueryOrDF
    from sqlmesh.core.model.kind import TimeColumn
    from sqlmesh.core.node import IntervalUnit
    from sqlmesh.core.schema_diff import TableAlterOperation
    from sqlmesh.utils.date import TimeLike

logger = logging.getLogger(__name__)


@set_catalog()
class DorisEngineAdapter(
    EngineAdapterWithIndexSupport,
    GetCurrentCatalogFromFunctionMixin,
    PandasNativeFetchDFSupportMixin,
    RowDiffMixin,
):
    DIALECT = "doris"
    DEFAULT_BATCH_SIZE = 500
    SUPPORTS_TRANSACTIONS = False
    COMMENT_CREATION_TABLE = CommentCreationTable.IN_SCHEMA_DEF_NO_CTAS
    COMMENT_CREATION_VIEW = CommentCreationView.IN_SCHEMA_DEF_NO_COMMANDS
    MAX_TABLE_COMMENT_LENGTH = 2048
    MAX_COLUMN_COMMENT_LENGTH = 255
    SUPPORTS_INDEXES = True
    SUPPORTS_TUPLE_IN = False
    SUPPORTS_REPLACE_TABLE = False
    MAX_IDENTIFIER_LENGTH = 64
    SUPPORTS_MATERIALIZED_VIEWS = True
    SUPPORTS_MATERIALIZED_VIEW_SCHEMA = True
    RECREATE_MATERIALIZED_VIEW_ON_EVALUATION = False
    SUPPORTS_CREATE_DROP_CATALOG = False
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    INSERT_OVERWRITE_STRATEGY = InsertOverwriteStrategy.DELETE_INSERT
    CURRENT_CATALOG_EXPRESSION = exp.func("current_catalog")
    RESOLVE_TABLE_REFS_IN_PHYSICAL_PROPERTIES: t.FrozenSet[str] = frozenset(
        {"excluded_trigger_tables", "source_table"}
    )
    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(9, 0), (0,)],
            exp.DataType.build("CHAR", dialect=DIALECT).this: [(1,), (255,)],
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: [(65533,)],
            exp.DataType.build("DATETIME", dialect=DIALECT).this: [(0,)],
            exp.DataType.build("TIMESTAMPTZ", dialect=DIALECT).this: [(0,)],
            exp.DataType.build("INT", dialect=DIALECT).this: [(11,)],
            exp.DataType.build("BIGINT", dialect=DIALECT).this: [(20,)],
            exp.DataType.build("SMALLINT", dialect=DIALECT).this: [(6,)],
            exp.DataType.build("TINYINT", dialect=DIALECT).this: [(4,), (1,)],
        },
        "types_with_unlimited_length": {
            exp.DataType.build("TEXT", dialect=DIALECT).this: {
                exp.DataType.build("VARCHAR", dialect=DIALECT).this,
                exp.DataType.build("CHAR", dialect=DIALECT).this,
            },
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: {
                exp.DataType.build("VARCHAR", dialect=DIALECT).this,
                exp.DataType.build("CHAR", dialect=DIALECT).this,
                exp.DataType.build("TEXT", dialect=DIALECT).this,
            },
        },
        "compatible_types": {
            exp.DataType.build("BOOLEAN", dialect=DIALECT): {
                exp.DataType.build("TINYINT", dialect=DIALECT),
                exp.DataType.build("SMALLINT", dialect=DIALECT),
                exp.DataType.build("INT", dialect=DIALECT),
                exp.DataType.build("BIGINT", dialect=DIALECT),
                exp.DataType.build("INT128", dialect=DIALECT),
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("TINYINT", dialect=DIALECT): {
                exp.DataType.build("SMALLINT", dialect=DIALECT),
                exp.DataType.build("INT", dialect=DIALECT),
                exp.DataType.build("BIGINT", dialect=DIALECT),
                exp.DataType.build("INT128", dialect=DIALECT),
                exp.DataType.build("DOUBLE", dialect=DIALECT),
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("SMALLINT", dialect=DIALECT): {
                exp.DataType.build("INT", dialect=DIALECT),
                exp.DataType.build("BIGINT", dialect=DIALECT),
                exp.DataType.build("INT128", dialect=DIALECT),
                exp.DataType.build("DOUBLE", dialect=DIALECT),
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("INT", dialect=DIALECT): {
                exp.DataType.build("BIGINT", dialect=DIALECT),
                exp.DataType.build("INT128", dialect=DIALECT),
                exp.DataType.build("DOUBLE", dialect=DIALECT),
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("BIGINT", dialect=DIALECT): {
                exp.DataType.build("INT128", dialect=DIALECT),
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("INT128", dialect=DIALECT): {
                exp.DataType.build("LARGEINT", dialect=DIALECT, udt=True),
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("LARGEINT", dialect=DIALECT, udt=True): {
                exp.DataType.build("INT128", dialect=DIALECT),
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("FLOAT", dialect=DIALECT): {
                exp.DataType.build("DOUBLE", dialect=DIALECT),
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("DOUBLE", dialect=DIALECT): {
                exp.DataType.build("TEXT", dialect=DIALECT),
            },
            exp.DataType.build("DATE", dialect=DIALECT): {
                exp.DataType.build("DATETIME", dialect=DIALECT),
                *(exp.DataType.build(f"DATETIME({p})", dialect="doris") for p in range(0, 7)),
            },
            exp.DataType.build("DATETIME", dialect=DIALECT): {
                *(exp.DataType.build(f"DATETIME({p})", dialect="doris") for p in range(0, 7)),
            },
            exp.DataType.build("DATETIME(0)", dialect=DIALECT): {
                exp.DataType.build("DATETIME", dialect=DIALECT),
                *(exp.DataType.build(f"DATETIME({p})", dialect="doris") for p in range(1, 7)),
            },
            exp.DataType.build("TIMESTAMPTZ", dialect=DIALECT): {
                *(exp.DataType.build(f"TIMESTAMPTZ({p})", dialect="doris") for p in range(0, 7)),
            },
            exp.DataType.build("TIMESTAMPTZ(0)", dialect=DIALECT): {
                exp.DataType.build("TIMESTAMPTZ", dialect=DIALECT),
                *(exp.DataType.build(f"TIMESTAMPTZ({p})", dialect="doris") for p in range(1, 7)),
            },
            **{
                exp.DataType.build(f"DATETIME({p})", dialect="doris"): {
                    exp.DataType.build(f"DATETIME({p2})", dialect="doris") for p2 in range(p + 1, 7)
                }
                for p in range(1, 6)
            },
            **{
                exp.DataType.build(f"TIMESTAMPTZ({p})", dialect="doris"): {
                    exp.DataType.build(f"TIMESTAMPTZ({p2})", dialect="doris")
                    for p2 in range(p + 1, 7)
                }
                for p in range(1, 6)
            },
        },
    }

    INVALID_KEY_TYPES = {
        "FLOAT",
        "DOUBLE",
        "STRING",
        "TEXT",
        "TINYTEXT",
        "MEDIUMTEXT",
        "LONGTEXT",
        "BLOB",
        "TINYBLOB",
        "MEDIUMBLOB",
        "LONGBLOB",
        "BINARY",
        "VARBINARY",
        "JSON",
        "JSONB",
        "VARIANT",
        "OBJECT",
        "ARRAY",
        "MAP",
        "STRUCT",
        "BITMAP",
        "HLL",
        "HLLSKETCH",
        "QUANTILE_STATE",
        "AGG_STATE",
    }

    def _get_thread_state(self) -> threading.local:
        state = getattr(self._connection_pool, "_doris_thread_state", None)
        if not isinstance(state, threading.local):
            state = self._extra_config.get("_doris_thread_state")
            if not isinstance(state, threading.local):
                state = getattr(self, "_doris_thread_state", None)
                if not isinstance(state, threading.local):
                    state = threading.local()
            self._doris_thread_state = state
            if isinstance(getattr(self, "_extra_config", None), dict):
                self._extra_config["_doris_thread_state"] = state
            try:
                setattr(self._connection_pool, "_doris_thread_state", state)
            except Exception:
                pass
        return state

    @property
    def _active_catalog(self) -> t.Optional[str]:
        return getattr(self._get_thread_state(), "active_catalog", None)

    @_active_catalog.setter
    def _active_catalog(self, value: t.Optional[str]) -> None:
        self._get_thread_state().active_catalog = value

    @property
    def _needs_catalog_restore(self) -> bool:
        return bool(getattr(self._get_thread_state(), "needs_catalog_restore", False))

    @_needs_catalog_restore.setter
    def _needs_catalog_restore(self, value: bool) -> None:
        self._get_thread_state().needs_catalog_restore = bool(value)

    @property
    def _last_described_key_cols(self) -> t.Set[str]:
        return getattr(self._get_thread_state(), "last_described_key_cols", set())

    @_last_described_key_cols.setter
    def _last_described_key_cols(self, value: t.Set[str]) -> None:
        self._get_thread_state().last_described_key_cols = set(value)

    @property
    def _in_iceberg_replace_by_key(self) -> bool:
        return bool(getattr(self._get_thread_state(), "in_iceberg_replace_by_key", False))

    @_in_iceberg_replace_by_key.setter
    def _in_iceberg_replace_by_key(self, value: bool) -> None:
        self._get_thread_state().in_iceberg_replace_by_key = bool(value)

    @staticmethod
    def _get_table_properties(kwargs: t.Dict[str, t.Any]) -> t.Optional[t.Dict[str, t.Any]]:
        return kwargs.get("table_properties") or kwargs.get("physical_properties")

    @property
    def _sync_mv_source_tables(self) -> t.Dict[str, exp.Table]:
        registry = getattr(self._connection_pool, "_doris_sync_mv_source_tables", None)
        if not isinstance(registry, dict):
            registry = self._extra_config.get("_doris_sync_mv_source_tables")
            if not isinstance(registry, dict):
                registry = getattr(self, "_doris_sync_mv_source_tables", None)
                if not isinstance(registry, dict):
                    registry = {}
            self._doris_sync_mv_source_tables = registry
            if isinstance(getattr(self, "_extra_config", None), dict):
                self._extra_config["_doris_sync_mv_source_tables"] = registry
            try:
                setattr(self._connection_pool, "_doris_sync_mv_source_tables", registry)
            except Exception:
                pass
        return registry

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.FULL_SUPPORT

    @property
    def default_catalog(self) -> t.Optional[str]:
        return self._default_catalog or self._extra_config.get("catalog")

    def get_current_catalog(self) -> t.Optional[str]:
        try:
            catalog = super().get_current_catalog()
            if catalog:
                return _decoded_str(catalog)
        except Exception:
            logger.debug(
                "current_catalog() failed on Doris, falling back to active/default catalog"
            )
        return (
            self._active_catalog
            or getattr(self._connection_pool, "_doris_last_switched_catalog", None)
            or self._extra_config.get("_doris_last_switched_catalog")
            or getattr(self, "_doris_last_switched_catalog", None)
            or self.default_catalog
            or "internal"
        )

    def set_current_catalog(self, catalog: str) -> None:
        catalog_sql = self._to_sql(exp.to_identifier(catalog, quoted=True))
        self.execute(exp.Command(this="SWITCH", expression=catalog_sql))
        self._active_catalog = catalog
        self._doris_last_switched_catalog = catalog
        if isinstance(getattr(self, "_extra_config", None), dict):
            self._extra_config["_doris_last_switched_catalog"] = catalog
        try:
            setattr(self._connection_pool, "_doris_last_switched_catalog", catalog)
        except Exception:
            pass
        self._needs_catalog_restore = False
        database = self._extra_config.get("database")
        default_cat = self.default_catalog or "internal"
        if database and catalog.lower() == str(default_cat).lower():
            db_sql = self._to_sql(exp.to_identifier(str(database), quoted=True))
            self.execute(f"USE {db_sql}")

    def _restore_active_catalog_on_reconnect(self, sql: t.Optional[str] = None) -> None:
        active_cat = (
            self._active_catalog
            or getattr(self._connection_pool, "_doris_last_switched_catalog", None)
            or self._extra_config.get("_doris_last_switched_catalog")
            or getattr(self, "_doris_last_switched_catalog", None)
            or self.default_catalog
        )
        database = self._extra_config.get("database")
        if not active_cat and not database:
            self._needs_catalog_restore = False
            return
        if sql and re.match(
            r"\A(?:\s|/\*.*?\*/|--[^\r\n]*(?:[\r\n]|\Z))*SWITCH\b",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            self._needs_catalog_restore = False
            return
        if active_cat:
            catalog_sql = self._to_sql(exp.to_identifier(active_cat, quoted=True))
            self.cursor.execute(f"SWITCH {catalog_sql}")
        default_cat = self.default_catalog or "internal"
        if database and (not active_cat or active_cat.lower() == str(default_cat).lower()):
            db_sql = self._to_sql(exp.to_identifier(str(database), quoted=True))
            self.cursor.execute(f"USE {db_sql}")
        self._needs_catalog_restore = False

    _READ_ONLY_EXTERNAL_FORMATS: t.ClassVar[t.Dict[str, str]] = {
        "lance": "Lance",
        "hudi": "Hudi",
        "deltalake": "Delta Lake",
        "delta": "Delta Lake",
        "lakesoul": "LakeSoul",
    }

    def _get_read_only_external_format(
        self,
        table_format: t.Optional[str] = None,
        table_properties: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> t.Optional[str]:
        if table_format:
            fmt_lower = str(table_format).strip().lower()
            if fmt_lower in self._READ_ONLY_EXTERNAL_FORMATS:
                return fmt_lower
        if table_properties:
            engine_val = self._extract_str_value(table_properties.get("engine"))
            if engine_val:
                eng_lower = engine_val.strip().lower()
                if eng_lower in self._READ_ONLY_EXTERNAL_FORMATS:
                    return eng_lower
        return None

    def _is_lance_table(
        self,
        table_format: t.Optional[str] = None,
        table_properties: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> bool:
        return (
            self._get_read_only_external_format(
                table_format=table_format, table_properties=table_properties
            )
            is not None
        )

    def _ensure_not_lance_table(
        self,
        table_format: t.Optional[str] = None,
        table_properties: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> None:
        ro_fmt = self._get_read_only_external_format(
            table_format=table_format, table_properties=table_properties
        )
        if ro_fmt is not None:
            fmt_label = self._READ_ONLY_EXTERNAL_FORMATS[ro_fmt]
            raise SQLMeshError(
                f"Doris {fmt_label} tables are read-only; reference {fmt_label} tables as upstream sources or EXTERNAL models "
                f"using a Doris {fmt_label} catalog ('type' = '{ro_fmt}') or file table-valued function ('format' = '{ro_fmt}')."
            )

    def _is_iceberg_table(
        self,
        table_format: t.Optional[str] = None,
        catalog_name: t.Optional[str] = None,
        table_properties: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> bool:
        if self._is_lance_table(table_format=table_format, table_properties=table_properties):
            return False
        if table_format and str(table_format).strip().lower() in ("iceberg", "paimon", "hive"):
            return True
        if table_properties:
            engine_val = self._extract_str_value(table_properties.get("engine"))
            if engine_val and engine_val.strip().lower() in ("iceberg", "paimon", "hive"):
                return True
        effective_catalog = catalog_name or self.default_catalog
        if effective_catalog and str(effective_catalog).strip().lower() != "internal":
            return True
        return False

    @staticmethod
    def _extract_str_value(val: t.Any) -> t.Optional[str]:
        if val is None:
            return None
        if isinstance(val, exp.Paren):
            return DorisEngineAdapter._extract_str_value(val.this)
        if isinstance(val, (exp.Tuple, exp.Array)):
            return ",".join(
                s for e in val.expressions if (s := DorisEngineAdapter._extract_str_value(e))
            )
        if isinstance(val, exp.Boolean):
            return "true" if val.this else "false"
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, exp.Literal):
            return str(val.this)
        if isinstance(val, exp.Identifier):
            return str(val.this)
        if isinstance(val, (exp.Column, exp.Table)):
            return ".".join(part.name for part in val.parts)
        if isinstance(val, exp.Var):
            return str(val.this)
        if isinstance(val, str):
            return val
        if isinstance(val, exp.Expr):
            return val.name or str(val)
        return str(val)

    @staticmethod
    def _split_cleaned_column_names(raw: str) -> t.List[str]:
        s = raw.strip()
        while len(s) >= 2 and (
            (s[0] == "(" and s[-1] == ")")
            or (s[0] == "'" and s[-1] == "'")
            or (s[0] == '"' and s[-1] == '"')
        ):
            s = s[1:-1].strip()
        result: t.List[str] = []
        for part in s.split(","):
            cleaned = part.strip().strip("'\"").strip("()").strip().strip("`'\"").strip()
            if cleaned:
                result.append(cleaned)
        return result

    @classmethod
    def _extract_column_names(cls, key_expr: t.Any) -> t.List[str]:
        if key_expr is None:
            return []
        if isinstance(key_expr, (exp.Tuple, exp.Array)):
            names: t.List[str] = []
            for e in key_expr.expressions:
                names.extend(cls._extract_column_names(e))
            return names
        if isinstance(key_expr, exp.Paren):
            return cls._extract_column_names(key_expr.this)
        if isinstance(key_expr, (list, tuple)):
            names = []
            for e in key_expr:
                names.extend(cls._extract_column_names(e))
            return names
        if isinstance(key_expr, exp.Column):
            return cls._split_cleaned_column_names(key_expr.name)
        if isinstance(key_expr, (exp.Identifier, exp.Literal, exp.Var)):
            return cls._split_cleaned_column_names(str(key_expr.this))
        if isinstance(key_expr, exp.Expr):
            return []
        if isinstance(key_expr, str):
            return cls._split_cleaned_column_names(key_expr)
        return cls._split_cleaned_column_names(str(key_expr))

    @classmethod
    def _is_valid_olap_key_type(cls, dtype: exp.DataType) -> bool:
        type_name = (
            dtype.this.value.upper()
            if isinstance(dtype.this, exp.DataType.Type)
            else str(dtype.this).upper()
        )
        if type_name in cls.INVALID_KEY_TYPES:
            return False
        if dtype.this == exp.DataType.Type.USERDEFINED:
            kind = (
                str(dtype.args.get("kind") or dtype.sql(dialect=cls.DIALECT))
                .upper()
                .split("(", 1)[0]
                .strip()
                .strip("`")
            )
            if kind in cls.INVALID_KEY_TYPES:
                return False
        return True

    def _normalize_nested_value(self, expr: exp.Expr) -> exp.Expr:
        return exp.cast(expr, exp.DataType.build("VARCHAR", dialect=self.dialect))

    def normalize_value(
        self,
        expr: exp.Expr,
        type: exp.DataType,
        decimal_precision: int = 3,
        timestamp_precision: int = 6,
    ) -> exp.Expr:
        udt_kind = (
            str(type.args.get("kind") or "").upper().split("(", 1)[0].strip().strip("`")
            if type.this == exp.DataType.Type.USERDEFINED
            else ""
        )
        if type.is_type("int128") or udt_kind == "LARGEINT":
            return exp.cast(
                exp.cast(expr, exp.DataType.build("LARGEINT", udt=True)),
                to=exp.DataType.build("VARCHAR"),
            )
        if udt_kind == "BITMAP":
            return exp.cast(
                exp.func("BITMAP_TO_STRING", expr),
                to=exp.DataType.build("VARCHAR"),
            )
        if type.is_type("hllsketch") or udt_kind in ("HLL", "HLLSKETCH"):
            return exp.cast(
                exp.func("HLL_TO_BASE64", expr),
                to=exp.DataType.build("VARCHAR"),
            )
        return super().normalize_value(
            expr,
            type,
            decimal_precision=decimal_precision,
            timestamp_precision=timestamp_precision,
        )

    def _fetch_native_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> DF:
        if isinstance(query, exp.Expr):
            query = self._normalize_schema_tmp_identifiers(query)
        elif isinstance(query, str) and (
            "_schema_tmp" in query
            or any(
                tok in query.upper()
                for tok in ("IS TRUE", "IS FALSE", "IS NOT TRUE", "IS NOT FALSE")
            )
        ):
            try:
                parsed_q = parse_one(query, read=self.dialect)
                query = self._to_sql(parsed_q, quote=quote_identifiers)
            except Exception:
                pass
        if self._pre_ping:
            try:
                self.ping()
            except Exception:
                logger.info("Connection to the database was lost. Reconnecting...")
                self._connection_pool.close()
                self._needs_catalog_restore = True
        try:
            self._connection_pool.get_cursor()
            if getattr(self, "_needs_catalog_restore", False):
                self._restore_active_catalog_on_reconnect()
            return super()._fetch_native_df(query, quote_identifiers=quote_identifiers)
        except Exception as e:
            err_code = e.args[0] if getattr(e, "args", None) else None
            err_name = e.__class__.__name__
            is_disconnect = (err_code in (2006, 2013, 2055) and err_name == "OperationalError") or (
                err_code == 0 and err_name == "InterfaceError"
            )
            is_timeout = (
                isinstance(getattr(e, "__context__", None), TimeoutError)
                or "timed out" in str(e).lower()
            )
            if (
                is_disconnect
                and not is_timeout
                and not self._connection_pool.is_transaction_active
                and self._is_safe_to_retry_sql(
                    self._to_sql(query, quote=quote_identifiers)
                    if isinstance(query, exp.Expr)
                    else query
                )
            ):
                logger.warning(
                    "Doris connection lost (%s: %s) during fetchdf, reconnecting and retrying once...",
                    err_name,
                    err_code,
                )
                self._connection_pool.close()
                try:
                    self._connection_pool.get_cursor()
                    self._restore_active_catalog_on_reconnect()
                    return super()._fetch_native_df(query, quote_identifiers=quote_identifiers)
                except Exception:
                    self._connection_pool.close()
                    self._needs_catalog_restore = True
                    raise
            if is_disconnect and not self._connection_pool.is_transaction_active:
                self._connection_pool.close()
                self._needs_catalog_restore = True
            raise

    _SAFE_RETRY_SQL_PREFIX_RE = re.compile(
        r"\A(?:\s|/\*.*?\*/|--[^\r\n]*(?:[\r\n]|\Z))*(SELECT|WITH|SHOW|DESCRIBE|DESC|EXPLAIN|USE|SWITCH)\b",
        re.IGNORECASE | re.DOTALL,
    )

    @classmethod
    def _is_safe_to_retry_sql(cls, sql: str) -> bool:
        """Returns True only for read-only or idempotent catalog/database switch statements that cannot duplicate writes."""
        match = cls._SAFE_RETRY_SQL_PREFIX_RE.match(sql)
        if not match:
            return False
        if match.group(1).upper() == "WITH":
            try:
                parsed = parse_one(sql, read=cls.DIALECT)
            except Exception:
                return False
            return isinstance(parsed, (exp.Select, exp.SetOperation))
        return True

    def ping(self) -> None:
        self._connection_pool.get().ping(reconnect=False)

    @classmethod
    def _shorten_schema_tmp_name(cls, name: str) -> str:
        max_len = cls.MAX_IDENTIFIER_LENGTH or 64
        suffix = "_schema_tmp"
        if len(name) > max_len and name.endswith(suffix):
            digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
            prefix_len = max_len - len(suffix) - 9
            return f"{name[:prefix_len]}_{digest}{suffix}"
        return name

    @classmethod
    def _normalize_schema_tmp_identifiers(cls, expression: exp.Expr) -> exp.Expr:
        max_len = cls.MAX_IDENTIFIER_LENGTH or 64
        suffix = "_schema_tmp"
        if not any(
            len(ident.name) > max_len and ident.name.endswith(suffix)
            for ident in expression.find_all(exp.Identifier)
        ):
            return expression
        expression = expression.copy()
        for ident in expression.find_all(exp.Identifier):
            if len(ident.name) > max_len and ident.name.endswith(suffix):
                ident.set("this", cls._shorten_schema_tmp_name(ident.name))
        return expression

    def execute(
        self,
        expressions: t.Union[str, exp.Expr, t.Sequence[exp.Expr]],
        ignore_unsupported_errors: bool = False,
        quote_identifiers: bool = True,
        track_rows_processed: bool = False,
        **kwargs: t.Any,
    ) -> None:
        if isinstance(expressions, str):
            super().execute(
                expressions,
                ignore_unsupported_errors=ignore_unsupported_errors,
                quote_identifiers=quote_identifiers,
                track_rows_processed=track_rows_processed,
                **kwargs,
            )
            return

        processed_expressions: t.List[exp.Expr] = []
        for e in ensure_list(expressions):
            if not isinstance(e, exp.Expr):
                super().execute(
                    expressions,
                    ignore_unsupported_errors=ignore_unsupported_errors,
                    quote_identifiers=quote_identifiers,
                    track_rows_processed=track_rows_processed,
                    **kwargs,
                )
                return
            if isinstance(e, exp.Select) and e.args.get("locks"):
                e = e.copy()
                e.set("locks", None)
                logger.warning(
                    "[Doris] Removed FOR UPDATE from SELECT statement: %s",
                    e.sql(dialect=self.dialect, identify=quote_identifiers),
                )
            e = self._normalize_schema_tmp_identifiers(e)
            processed_expressions.append(e)

        super().execute(
            processed_expressions,
            ignore_unsupported_errors=ignore_unsupported_errors,
            quote_identifiers=quote_identifiers,
            track_rows_processed=track_rows_processed,
            **kwargs,
        )

    def _execute(self, sql: str, track_rows_processed: bool = False, **kwargs: t.Any) -> None:
        if self._pre_ping:
            try:
                self.ping()
            except Exception:
                logger.info("Connection to the database was lost. Reconnecting...")
                self._connection_pool.close()
                self._needs_catalog_restore = True
        try:
            if getattr(self, "_needs_catalog_restore", False):
                self._restore_active_catalog_on_reconnect(sql)
            super()._execute(sql, track_rows_processed=track_rows_processed, **kwargs)
        except Exception as e:
            err_code = e.args[0] if getattr(e, "args", None) else None
            err_name = e.__class__.__name__
            is_disconnect = (err_code in (2006, 2013, 2055) and err_name == "OperationalError") or (
                err_code == 0 and err_name == "InterfaceError"
            )
            is_timeout = (
                isinstance(getattr(e, "__context__", None), TimeoutError)
                or "timed out" in str(e).lower()
            )
            if (
                is_disconnect
                and not is_timeout
                and not self._connection_pool.is_transaction_active
                and self._is_safe_to_retry_sql(sql)
            ):
                logger.warning(
                    "Doris connection lost (%s: %s) during read/session statement, reconnecting and retrying once...",
                    err_name,
                    err_code,
                )
                self._connection_pool.close_cursor()
                try:
                    conn = self._connection_pool.get()
                    try:
                        conn.ping(reconnect=True)
                    except Exception:
                        self._connection_pool.close()
                    self._restore_active_catalog_on_reconnect(sql)
                    super()._execute(sql, track_rows_processed=track_rows_processed, **kwargs)
                except Exception:
                    self._connection_pool.close()
                    self._needs_catalog_restore = True
                    raise
            else:
                if is_disconnect and not self._connection_pool.is_transaction_active:
                    self._connection_pool.close()
                    self._needs_catalog_restore = True
                raise

    def columns(
        self, table_name: TableName, include_pseudo_columns: bool = False
    ) -> t.Dict[str, exp.DataType]:
        """Fetches column names and types for the target table, normalizing Doris type aliases."""
        cols, key_cols = self._describe_columns_with_keys(table_name)
        self._last_described_key_cols: t.Set[str] = key_cols
        return cols

    def _describe_columns_with_keys(
        self, table_name: TableName
    ) -> t.Tuple[t.Dict[str, exp.DataType], t.Set[str]]:
        self.execute(exp.Describe(this=exp.to_table(table_name), kind="TABLE"))
        describe_output = self.cursor.fetchall()
        cols: t.Dict[str, exp.DataType] = {}
        key_cols: t.Set[str] = set()
        has_explicit_keys = False
        for row in itertools.takewhile(
            lambda r: not str(r[0]).startswith("#"),
            describe_output,
        ):
            column_name = row[0] if len(row) > 0 else None
            column_type = row[1] if len(row) > 1 else None
            is_key = row[3] if len(row) > 3 else None
            col_name_str = _decoded_str(column_name) if column_name else ""
            raw_type = _decoded_str(column_type) if column_type else ""
            if not col_name_str.strip() or not raw_type.strip():
                continue
            if is_key is not None:
                has_explicit_keys = True
                if _decoded_str(is_key).strip().lower() in (
                    "true",
                    "pri",
                    "uni",
                    "dup",
                    "agg",
                    "yes",
                ):
                    key_cols.add(col_name_str.lower())
            is_agg_state = raw_type.strip().upper().startswith("AGG_STATE")
            normalized_type = re.sub(
                r"\bDECIMALV[23]\b(?!\s*`?\s*:)", "DECIMAL", raw_type, flags=re.IGNORECASE
            )
            normalized_type = re.sub(
                r"\bDATETIMEV2\b(?!\s*`?\s*:)", "DATETIME", normalized_type, flags=re.IGNORECASE
            )
            normalized_type = re.sub(
                r"\bDATEV2\b(?!\s*`?\s*:)", "DATE", normalized_type, flags=re.IGNORECASE
            )
            if not is_agg_state:
                normalized_type = re.sub(
                    r"\bLARGEINT(?:\(\d+\)|\b)(?!\s*`?\s*:)",
                    "INT128",
                    normalized_type,
                    flags=re.IGNORECASE,
                )
            else:
                normalized_type = re.sub(
                    r"\s+(?:NOT\s+)?NULL\b",
                    "",
                    normalized_type,
                    flags=re.IGNORECASE,
                )
            normalized_type = re.sub(
                r"\bTINYINT\(1\)", "BOOLEAN", normalized_type, flags=re.IGNORECASE
            )
            normalized_type = re.sub(
                r"\b(TINYINT|SMALLINT|INT|BIGINT)\(\d+\)",
                r"\1",
                normalized_type,
                flags=re.IGNORECASE,
            )
            dtype = exp.DataType.build(normalized_type, dialect=self.dialect, udt=True)
            for col_def in dtype.find_all(exp.ColumnDef):
                if isinstance(col_def.this, exp.Identifier) and col_def.this.quoted:
                    col_def.this.set("quoted", False)
                constraints = [
                    c
                    for c in (col_def.args.get("constraints") or [])
                    if not (
                        isinstance(c, exp.NotNullColumnConstraint)
                        and bool(c.args.get("allow_null"))
                    )
                    and not (
                        isinstance(getattr(c, "kind", None), exp.NotNullColumnConstraint)
                        and bool(c.kind.args.get("allow_null"))
                    )
                ]
                col_def.set("constraints", constraints or None)
            cols[col_name_str] = dtype
        self._last_described_has_explicit_keys: bool = has_explicit_keys
        if cols and not key_cols and not has_explicit_keys:
            first_col = next(iter(cols))
            key_cols.add(first_col.lower())
        return cols, key_cols

    def get_alter_operations(
        self,
        current_table_name: TableName,
        target_table_name: TableName,
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterOperation]:
        from sqlmesh.core.schema_diff import (
            TableAlterAddColumnOperation,
            TableAlterChangeColumnTypeOperation,
            TableAlterDropColumnOperation,
            TableAlterOperation,
        )

        current_table = exp.to_table(current_table_name)
        is_iceberg = self._is_iceberg_table(catalog_name=current_table.catalog)
        if is_iceberg:
            iceberg_ops = super().get_alter_operations(
                current_table_name,
                target_table_name,
                ignore_destructive=ignore_destructive,
                ignore_additive=ignore_additive,
            )
            return [
                op
                for op in iceberg_ops
                if not (
                    isinstance(op, TableAlterChangeColumnTypeOperation)
                    and self._to_sql(op.current_type).upper()
                    == self._to_sql(op.column_type).upper()
                )
            ]

        self._last_described_key_cols = set()
        self._last_described_has_explicit_keys = False
        current_cols = self.columns(current_table_name)
        current_key_cols = set(getattr(self, "_last_described_key_cols", set()))
        current_has_explicit_keys = bool(getattr(self, "_last_described_has_explicit_keys", False))
        if current_cols and not current_key_cols and not current_has_explicit_keys:
            current_key_cols = {next(iter(current_cols)).lower()}

        self._last_described_key_cols = set()
        self._last_described_has_explicit_keys = False
        target_cols = self.columns(target_table_name)
        target_key_cols = set(getattr(self, "_last_described_key_cols", set()))
        target_has_explicit_keys = bool(getattr(self, "_last_described_has_explicit_keys", False))
        if target_cols and not target_key_cols and not target_has_explicit_keys:
            target_key_cols = {next(iter(target_cols)).lower()}

        current_by_lower = {c.lower(): c_dtype for c, c_dtype in current_cols.items()}
        for t_col, t_dtype in list(target_cols.items()):
            curr_dtype = current_by_lower.get(t_col.lower())
            if (
                curr_dtype is not None
                and curr_dtype != t_dtype
                and self._to_sql(curr_dtype).upper() == self._to_sql(t_dtype).upper()
            ):
                target_cols[t_col] = curr_dtype.copy()

        key_role_changed_cols: t.Set[str] = set()
        if current_has_explicit_keys and target_has_explicit_keys:
            common_cols = {c.lower() for c in current_cols} & {c.lower() for c in target_cols}
            key_role_changed_cols = (current_key_cols ^ target_key_cols) & common_cols

        invalid_target_key_cols = {
            c.lower()
            for c, dtype in target_cols.items()
            if c.lower() in current_key_cols and not self._is_valid_olap_key_type(dtype)
        }
        operations = t.cast(
            t.List[TableAlterOperation],
            self.schema_differ.compare_columns(
                current_table_name,
                current_cols,
                target_cols,
                ignore_destructive=ignore_destructive,
                ignore_additive=ignore_additive,
            ),
        )
        dropped_key_cols = {
            op.column.name.lower()
            for op in operations
            if isinstance(op, TableAlterDropColumnOperation)
            and op.column.name.lower() in current_key_cols
        }
        protected_key_cols = invalid_target_key_cols | dropped_key_cols | key_role_changed_cols
        return [
            op
            for op in operations
            if not (
                (
                    isinstance(op, TableAlterChangeColumnTypeOperation)
                    and (
                        self._to_sql(op.current_type).upper()
                        == self._to_sql(op.column_type).upper()
                        or (
                            op.column.name.lower() in current_key_cols
                            and not self._is_valid_olap_key_type(op.column_type)
                        )
                        or op.column.name.lower() in key_role_changed_cols
                    )
                )
                or (
                    isinstance(op, (TableAlterDropColumnOperation, TableAlterAddColumnOperation))
                    and op.column.name.lower() in protected_key_cols
                )
            )
        ]

    def _wait_for_schema_change(self, table_name: TableName, timeout: float = 30.0) -> None:
        """Wait for any active asynchronous OLAP SCHEMA_CHANGE job on ``table_name`` to complete."""
        if type(self.cursor).__module__.startswith("unittest.mock"):
            return
        table = exp.to_table(table_name)
        if not table.name or self._is_iceberg_table(catalog_name=table.catalog):
            return
        escaped_tbl = table.name.replace("'", "''")
        from_clause = f" FROM `{table.db.replace('`', '``')}`" if table.db else ""
        show_sql = f"SHOW ALTER TABLE COLUMN{from_clause} WHERE TableName = '{escaped_tbl}'"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.cursor.execute(show_sql)
                rows = self.cursor.fetchall()
            except Exception:
                return
            if not rows:
                return
            if all(
                len(row) <= 9
                or str(_decoded_str(row[9])).strip().upper() in ("FINISHED", "CANCELLED")
                for row in rows
            ):
                return
            time.sleep(0.05)

    def alter_table(
        self,
        alter_expressions: t.Union[t.List[exp.Alter], t.List[TableAlterOperation]],
        table_format: t.Optional[str] = None,
    ) -> None:
        """Execute ALTER TABLE statements on Doris, waiting for asynchronous OLAP SCHEMA_CHANGE jobs."""
        from sqlmesh.core.schema_diff import TableAlterOperation

        with self.transaction():
            for alter_expression in [
                x.expression if isinstance(x, TableAlterOperation) else x for x in alter_expressions
            ]:
                for attempt in range(300):
                    try:
                        self.execute(alter_expression)
                        break
                    except Exception as e:
                        if "state(schema_change) is not normal" in str(e).lower() and attempt < 299:
                            if isinstance(alter_expression, exp.Alter) and alter_expression.this:
                                self._wait_for_schema_change(alter_expression.this, timeout=5.0)
                            else:
                                time.sleep(0.1)
                            continue
                        raise
                if (
                    isinstance(alter_expression, exp.Alter)
                    and alter_expression.this
                    and any(
                        isinstance(action, (exp.AlterColumn, exp.ColumnDef))
                        for action in (alter_expression.args.get("actions") or [])
                    )
                ):
                    self._wait_for_schema_change(alter_expression.this)

    def create_schema(
        self,
        schema_name: SchemaName,
        ignore_if_exists: bool = True,
        warn_on_error: bool = True,
        properties: t.Optional[t.List[exp.Expr]] = None,
    ) -> None:
        """Create a schema (database) in Doris."""
        properties = properties or []
        return super()._create_schema(
            schema_name=schema_name,
            ignore_if_exists=ignore_if_exists,
            warn_on_error=warn_on_error,
            properties=properties,
            kind="DATABASE",
        )

    def drop_schema(
        self,
        schema_name: SchemaName,
        ignore_if_not_exists: bool = True,
        cascade: bool = False,
        **drop_args: t.Dict[str, exp.Expr],
    ) -> None:
        """Drop schema in Doris. Note: Doris doesn't support CASCADE clause."""
        if cascade:
            schema_exp = to_schema(schema_name) if schema_name else exp.Table()
            effective_catalog = schema_exp.catalog or self.default_catalog
            if effective_catalog and str(effective_catalog).strip().lower() != "internal":
                try:
                    for obj in self._get_data_objects(schema_name):
                        if obj.type == DataObjectType.TABLE:
                            self.drop_table(obj.to_table(), exists=True)
                except Exception:
                    logger.debug("Failed to enumerate tables before drop_schema on %s", schema_name)
        return self._drop_object(
            name=schema_name,
            exists=ignore_if_not_exists,
            kind="DATABASE",
            cascade=False,
            **drop_args,
        )

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """Returns all the data objects that exist in the given schema and optional catalog."""
        schema_exp = to_schema(schema_name) if schema_name else exp.Table()
        catalog = schema_exp.catalog or None
        query_catalog = catalog or self.default_catalog

        query = (
            exp.select(
                exp.column("table_schema").as_("schema_name"),
                exp.column("table_name").as_("name"),
                exp.case()
                .when(
                    exp.column("table_type").eq("BASE TABLE"),
                    exp.Literal.string("table"),
                )
                .when(
                    exp.column("table_type").eq("SYSTEM VIEW"),
                    exp.Literal.string("view"),
                )
                .when(
                    exp.column("table_type").eq("VIEW"),
                    exp.Literal.string("view"),
                )
                .when(
                    exp.column("table_type").eq("MATERIALIZED VIEW"),
                    exp.Literal.string("materialized_view"),
                )
                .else_(exp.column("table_type"))
                .as_("type"),
            )
            .from_(exp.table_("tables", db="information_schema", catalog=query_catalog))
            .where(
                exp.column("table_schema").eq(
                    schema_exp.db if schema_exp.db else exp.func("DATABASE")
                )
            )
        )
        short_to_orig: t.Dict[str, str] = {}
        if object_names:
            lowered_names: t.List[str] = []
            for name in object_names:
                short_name = self._shorten_schema_tmp_name(name)
                if short_name != name:
                    short_to_orig[short_name.lower()] = name
                lowered_names.append(short_name.lower())
            query = query.where(exp.func("LOWER", exp.column("table_name")).isin(*lowered_names))

        result: t.List[DataObject] = []
        for row_schema, table_name, table_type in self.fetchall(query):
            try:
                schema_str = _decoded_str(row_schema) if row_schema is not None else schema_exp.db
                name_str = _decoded_str(table_name) if table_name is not None else "unknown"
                if name_str.lower() in short_to_orig:
                    name_str = short_to_orig[name_str.lower()]
                obj_type = _decoded_str(table_type) if table_type is not None else "table"

                upper_type = obj_type.upper()
                if upper_type in ("BASE TABLE", "EXTERNAL TABLE"):
                    obj_type = "table"
                elif upper_type in ("VIEW", "SYSTEM VIEW"):
                    obj_type = "view"
                elif upper_type in ("MATERIALIZED VIEW", "MATERIALIZED_VIEW"):
                    obj_type = "materialized_view"

                result.append(
                    DataObject(
                        catalog=catalog,
                        schema=schema_str,
                        name=name_str,
                        type=DataObjectType.from_str(obj_type),
                    )
                )
            except (ValueError, AttributeError) as e:
                logger.error(
                    f"Error processing row: {e}, row: {(row_schema, table_name, table_type)}"
                )
                continue

        # In Doris 2.1+, asynchronous materialized views (MTMVs) are stored as OLAP base tables
        # and report table_type = 'BASE TABLE' in information_schema.tables. Reclassify them via
        # the mv_infos() table-valued function so drop_data_object_on_type_mismatch and metadata
        # inspection recognize them as MATERIALIZED_VIEW instead of issuing DROP TABLE.
        if (not query_catalog or query_catalog.lower() == "internal") and any(
            obj.type.is_table for obj in result
        ):
            try:
                resolved_db = (
                    schema_exp.db
                    or next((obj.schema_name for obj in result if obj.schema_name), "")
                    or self._extra_config.get("database")
                )
                db_arg = (
                    self._to_sql(exp.Literal.string(str(resolved_db)), quote=False)
                    if resolved_db
                    else "DATABASE()"
                )
                mv_rows = self.fetchall(
                    f"SELECT Name FROM mv_infos('database' = {db_arg})",
                    quote_identifiers=False,
                )
                mv_names = {
                    _decoded_str(row[0]).lower() for row in mv_rows if len(row) == 1 and row[0]
                }
                if mv_names:
                    for obj in result:
                        if obj.name.lower() in mv_names:
                            obj.type = DataObjectType.MATERIALIZED_VIEW
            except Exception:
                logger.debug("mv_infos lookup failed on Doris, skipping MTMV reclassification")

        return result

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
        """
        Doris view creation behavior:
        - Regular VIEW: supports atomic `CREATE OR REPLACE VIEW` via `super().create_view`.
        - MATERIALIZED VIEW: does NOT support `CREATE OR REPLACE MATERIALIZED VIEW`, so `replace=True`
          drops any existing object/materialized view first (after validating the new query and properties).
        """
        target_table = exp.to_table(view_name)
        effective_catalog = target_table.catalog or self.default_catalog
        if effective_catalog and effective_catalog.lower() != "internal":
            view_kind = "materialized views" if materialized else "views"
            raise SQLMeshError(
                f"Doris only supports creating {view_kind} in the 'internal' catalog, got '{effective_catalog}' "
                f"for view '{self._to_sql(target_table)}'. Configure `virtual_environment_mode: dev_only` and "
                f"`environment_catalog_mapping` in your SQLMesh configuration to route virtual environment views "
                f"to the 'internal' catalog."
            )

        if not materialized:
            return super().create_view(
                view_name,
                query_or_df,
                target_columns_to_types,
                replace=replace,
                materialized=False,
                materialized_properties=materialized_properties,
                table_description=table_description,
                column_descriptions=column_descriptions,
                view_properties=view_properties,
                source_columns=source_columns,
                **create_kwargs,
            )

        self._create_materialized_view(
            view_name,
            query_or_df,
            target_columns_to_types=target_columns_to_types,
            replace=replace,
            materialized_properties=materialized_properties,
            table_description=table_description,
            column_descriptions=column_descriptions,
            view_properties=view_properties,
            source_columns=source_columns,
            **create_kwargs,
        )

    def _create_materialized_view(
        self,
        view_name: TableName,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        replace: bool = True,
        materialized_properties: t.Optional[t.Dict[str, t.Any]] = None,
        table_description: t.Optional[str] = None,
        column_descriptions: t.Optional[t.Dict[str, str]] = None,
        view_properties: t.Optional[t.Dict[str, exp.Expr]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **create_kwargs: t.Any,
    ) -> None:
        query_or_df = self._native_df_to_pandas_df(query_or_df)

        # Inline DataFrames as a SELECT ... FROM VALUES query rather than delegating DataFrame
        # handling to _get_source_queries_and_columns_to_types, which would otherwise materialize
        # a temporary table that gets dropped as soon as the SourceQuery context manager exits.
        if isinstance(query_or_df, pd.DataFrame):
            target_columns_to_types, source_columns = self._columns_to_types(
                query_or_df, target_columns_to_types, source_columns
            )
            if not target_columns_to_types:
                raise SQLMeshError("columns_to_types must be provided for dataframes")
            source_columns_to_types = get_source_columns_to_types(
                target_columns_to_types, source_columns
            )
            ordered_df = query_or_df[list(source_columns_to_types)]
            values: t.List[t.Tuple[t.Any, ...]] = list(
                ordered_df.itertuples(index=False, name=None)
            )
            query_or_df = self._values_to_sql(
                values,
                source_columns_to_types,
                batch_start=0,
                batch_end=len(values),
            )

        is_sync_mv = bool(
            view_properties
            and (
                (self._extract_str_value(view_properties.get("materialized_type")) or "").upper()
                == "SYNC"
            )
        )

        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            None if is_sync_mv else target_columns_to_types,
            batch_size=0,
            target_table=view_name,
            source_columns=source_columns,
        )
        if len(source_queries) != 1:
            raise SQLMeshError("Only one source query is supported for creating materialized views")

        with source_queries[0] as query:
            target_table = exp.to_table(view_name)

            schema_cols_to_types = target_columns_to_types
            if (
                not schema_cols_to_types
                and column_descriptions
                and not is_sync_mv
                and isinstance(query, exp.Query)
                and query.named_selects
                and "*" not in query.named_selects
            ):
                schema_cols_to_types = {
                    col: exp.DataType.build("UNKNOWN") for col in query.named_selects
                }

            schema: t.Union[exp.Table, exp.Schema] = target_table
            if schema_cols_to_types and not is_sync_mv:
                schema = self._build_schema_exp(
                    target_table,
                    schema_cols_to_types,
                    column_descriptions,
                    is_view=True,
                )

            partitioned_by = None
            if not is_sync_mv:
                if materialized_properties and materialized_properties.get("partitioned_by"):
                    partitioned_by = materialized_properties.get("partitioned_by")
                elif create_kwargs.get("partitioned_by"):
                    partitioned_by = create_kwargs.get("partitioned_by")

            clustered_by = None
            if not is_sync_mv:
                if materialized_properties and materialized_properties.get("clustered_by"):
                    clustered_by = materialized_properties.get("clustered_by")
                elif create_kwargs.get("clustered_by"):
                    clustered_by = create_kwargs.get("clustered_by")

            properties_exp = self._build_table_properties_exp(
                catalog_name=target_table.catalog,
                table_properties=view_properties,
                target_columns_to_types=target_columns_to_types,
                table_description=(
                    table_description
                    if not is_sync_mv
                    and self.COMMENT_CREATION_VIEW.supports_schema_def
                    and self.comments_enabled
                    else None
                ),
                partitioned_by=partitioned_by,
                clustered_by=clustered_by,
                partition_interval_unit=(
                    materialized_properties.get("partition_interval_unit")
                    if materialized_properties and not is_sync_mv
                    else None
                ),
                table_kind="MATERIALIZED_VIEW",
            )

            if is_sync_mv:
                query = query.copy()
                for tbl_node in query.find_all(exp.Table):
                    if tbl_node.catalog and tbl_node.catalog.lower() == "internal":
                        tbl_node.set("catalog", None)
                if isinstance(query, exp.Select):
                    base_col_names = {
                        col_node.name.lower()
                        for col_node in query.find_all(exp.Column)
                        if col_node.name
                    }
                    aliased_selects: t.List[exp.Expr] = []
                    for sel_expr in query.expressions:
                        if isinstance(sel_expr, exp.Column):
                            aliased_selects.append(
                                exp.alias_(sel_expr.copy(), f"mv_{sel_expr.name}", quoted=True)
                            )
                        elif isinstance(sel_expr, exp.Alias) and (
                            (
                                isinstance(sel_expr.this, exp.Column)
                                and sel_expr.alias == sel_expr.this.name
                            )
                            or (
                                sel_expr.alias
                                and sel_expr.alias.lower() in base_col_names
                                and not sel_expr.alias.lower().startswith("mv_")
                            )
                        ):
                            aliased_selects.append(
                                exp.alias_(
                                    sel_expr.this.copy(), f"mv_{sel_expr.alias}", quoted=True
                                )
                            )
                        else:
                            aliased_selects.append(sel_expr)
                    query.set("expressions", aliased_selects)

            sync_source_tbl: t.Optional[exp.Table] = None
            if is_sync_mv:
                if view_properties and view_properties.get("source_table"):
                    sync_source_tbl = self._to_source_table(view_properties["source_table"])
                else:
                    cte_names = {cte.alias.lower() for cte in query.find_all(exp.CTE) if cte.alias}
                    first_tbl = next(
                        (
                            t_node
                            for t_node in query.find_all(exp.Table)
                            if t_node.name and t_node.name.lower() not in cte_names
                        ),
                        None,
                    )
                    if first_tbl:
                        sync_source_tbl = self._to_source_table(first_tbl)

            # Doris does not support `CREATE OR REPLACE MATERIALIZED VIEW`, so drop first when `replace=True`
            # after query and table property validation has succeeded.
            if replace:
                self.drop_data_object_on_type_mismatch(
                    self.get_data_object(view_name), DataObjectType.MATERIALIZED_VIEW
                )
                drop_view_props = dict(view_properties) if view_properties else None
                if (
                    is_sync_mv
                    and drop_view_props is not None
                    and not drop_view_props.get("source_table")
                    and sync_source_tbl is not None
                ):
                    drop_view_props["source_table"] = sync_source_tbl
                self.drop_view(
                    view_name,
                    ignore_if_not_exists=True,
                    materialized=True,
                    view_properties=drop_view_props,
                )

            # Doris synchronous materialized views are single-table rollup indexes whose DDL syntax
            # (`CREATE MATERIALIZED VIEW <mv_name> AS SELECT ... FROM <db>.<table>`) forbids a
            # `<db>.` qualifier on `<mv_name>` and requires an active database on the session.
            sync_db_switched: t.Optional[str] = None
            if is_sync_mv:
                sync_db: t.Optional[str] = target_table.db or (
                    sync_source_tbl.db if sync_source_tbl else None
                )
                if sync_db:
                    db_sql = self._to_sql(exp.to_identifier(sync_db, quoted=True))
                    self.execute(f"USE {db_sql}")
                    sync_db_switched = sync_db
                unqualified_mv = target_table.copy()
                unqualified_mv.set("db", None)
                unqualified_mv.set("catalog", None)
                schema = unqualified_mv

            try:
                self.execute(
                    exp.Create(
                        this=schema,
                        kind="VIEW",
                        replace=False,
                        exists=(
                            bool(create_kwargs.get("exists", False)) if not is_sync_mv else False
                        ),
                        expression=query,
                        properties=properties_exp,
                    ),
                    quote_identifiers=self.QUOTE_IDENTIFIERS_IN_VIEWS,
                )
            finally:
                configured_db = self._extra_config.get("database")
                if (
                    sync_db_switched
                    and configured_db
                    and str(configured_db).lower() != sync_db_switched.lower()
                ):
                    restore_db_sql = self._to_sql(
                        exp.to_identifier(str(configured_db), quoted=True)
                    )
                    self.execute(f"USE {restore_db_sql}")
            registry = self._sync_mv_source_tables
            if is_sync_mv and sync_source_tbl is not None:
                if target_table.db:
                    registry[f"{target_table.db.lower()}.{target_table.name.lower()}"] = (
                        sync_source_tbl.copy()
                    )
                else:
                    registry[target_table.name.lower()] = sync_source_tbl.copy()
                    if sync_source_tbl.db:
                        registry[f"{sync_source_tbl.db.lower()}.{target_table.name.lower()}"] = (
                            sync_source_tbl.copy()
                        )
            elif not is_sync_mv:
                self._pop_sync_mv_source_table(target_table)
            raw_build = (
                view_properties.get("build")
                if view_properties and view_properties.get("build") is not None
                else (view_properties.get("build_mode") if view_properties else None)
            )
            build_mode = self._to_upper_str(raw_build) if raw_build is not None else None
            if (
                not is_sync_mv
                and build_mode == "DEFERRED"
                and bool((materialized_properties or {}).get("has_audits"))
            ):
                self.execute(f"REFRESH MATERIALIZED VIEW {self._to_sql(target_table)} AUTO")
            self._clear_data_object_cache(view_name)

    def _pop_sync_mv_source_table(self, target_table: exp.Table) -> t.Optional[exp.Table]:
        registry = self._sync_mv_source_tables
        name_lower = target_table.name.lower()
        if target_table.db:
            db_lower = target_table.db.lower()
            popped = registry.pop(f"{db_lower}.{name_lower}", None)
            unqual = registry.get(name_lower)
            if unqual is not None and (not unqual.db or unqual.db.lower() == db_lower):
                registry.pop(name_lower, None)
            return popped
        popped = registry.pop(name_lower, None)
        if popped is not None and popped.db:
            registry.pop(f"{popped.db.lower()}.{name_lower}", None)
        return popped

    def _to_source_table(self, raw_src: t.Any) -> exp.Table:
        if isinstance(raw_src, exp.Table):
            tbl = raw_src.copy()
        elif isinstance(raw_src, exp.Column):
            tbl = exp.table_(
                raw_src.name,
                db=raw_src.table or None,
                catalog=raw_src.db or None,
            )
        else:
            src_str = self._extract_str_value(raw_src) or str(raw_src)
            tbl = exp.to_table(src_str, dialect=self.dialect)
        if tbl.catalog and tbl.catalog.lower() == "internal":
            tbl.set("catalog", None)
        return tbl

    def drop_view(
        self,
        view_name: TableName,
        ignore_if_not_exists: bool = True,
        materialized: bool = False,
        **kwargs: t.Any,
    ) -> None:
        """Drop view in Doris."""
        target_table = exp.to_table(view_name)
        effective_catalog = target_table.catalog or self.default_catalog
        if effective_catalog and effective_catalog.lower() != "internal":
            return

        view_properties = kwargs.pop("view_properties", None)
        sync_source_table: t.Any = None
        is_explicit_async = False
        if materialized and view_properties:
            mat_type = self._extract_str_value(view_properties.get("materialized_type"))
            if mat_type and mat_type.upper() == "SYNC":
                sync_source_table = view_properties.get("source_table")
            elif mat_type and mat_type.upper() != "SYNC":
                is_explicit_async = True
        if materialized and sync_source_table is None and not is_explicit_async:
            registry = self._sync_mv_source_tables
            if target_table.db:
                sync_source_table = registry.get(
                    f"{target_table.db.lower()}.{target_table.name.lower()}"
                )
            else:
                sync_source_table = registry.get(target_table.name.lower())

        if sync_source_table is not None:
            source_tbl_exp = self._to_source_table(sync_source_table)
            # Synchronous materialized views require the target/source table's database
            # to be active on the session and an unqualified `<mv_name>` in `DROP MATERIALIZED VIEW <mv> ON <tbl>`.
            sync_db = target_table.db or source_tbl_exp.db
            if sync_db:
                db_sql = self._to_sql(exp.to_identifier(sync_db, quoted=True))
                self.execute(f"USE {db_sql}")
            unqualified_mv = target_table.copy()
            unqualified_mv.set("db", None)
            unqualified_mv.set("catalog", None)
            view_table_sql = self._to_sql(unqualified_mv)
            source_table_sql = self._to_sql(source_tbl_exp)
            exists_clause = "IF EXISTS " if ignore_if_not_exists else ""
            try:
                self.execute(
                    f"DROP MATERIALIZED VIEW {exists_clause}{view_table_sql} ON {source_table_sql}"
                )
            finally:
                configured_db = self._extra_config.get("database")
                if sync_db and configured_db and str(configured_db).lower() != sync_db.lower():
                    restore_db_sql = self._to_sql(
                        exp.to_identifier(str(configured_db), quoted=True)
                    )
                    self.execute(f"USE {restore_db_sql}")
            self._pop_sync_mv_source_table(target_table)
            self._clear_data_object_cache(view_name)
            return
        super().drop_view(view_name, ignore_if_not_exists, materialized, **kwargs)

    def create_index(
        self,
        table_name: TableName,
        index_name: str,
        columns: t.Sequence[str | exp.Expr] | t.Tuple[str, ...],
        exists: bool = True,
        index_type: t.Optional[str] = None,
        index_properties: t.Optional[t.Dict[str, t.Any]] = None,
        comment: t.Optional[str] = None,
    ) -> None:
        if len(columns) != 1:
            logger.warning(
                f"[Doris] Skipping CREATE INDEX {index_name} on {table_name}: "
                f"Doris only supports single-column indexes, got columns {columns}."
            )
            return
        if index_type or index_properties or comment:
            if not self.SUPPORTS_INDEXES:
                return
            using_var = exp.var(index_type.strip().upper()) if index_type else None
            with_storage = (
                [
                    exp.Property(
                        this=exp.Literal.string(str(k)),
                        value=exp.Literal.string(str(self._extract_str_value(v) or v)),
                    )
                    for k, v in index_properties.items()
                ]
                if index_properties
                else None
            )
            comment_node = (
                exp.CommentColumnConstraint(
                    this=exp.Literal.string(self._truncate_column_comment(comment.strip()))
                )
                if comment
                else None
            )
            self.execute(
                exp.Create(
                    this=exp.Index(
                        this=exp.to_identifier(index_name),
                        table=exp.to_table(table_name),
                        params=exp.IndexParameters(
                            columns=[
                                exp.to_column(c.name if isinstance(c, exp.Expr) else str(c))
                                for c in columns
                            ],
                            using=using_var,
                            with_storage=with_storage,
                            where=comment_node,
                        ),
                    ),
                    kind="INDEX",
                    exists=exists,
                )
            )
            self._wait_for_schema_change(table_name)
            return
        col_names = tuple(c.name if isinstance(c, exp.Expr) else str(c) for c in columns)
        super().create_index(table_name, index_name, col_names, exists=exists)
        self._wait_for_schema_change(table_name)

    def _create_table_like(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        exists: bool = True,
        **kwargs: t.Any,
    ) -> None:
        target_table = exp.to_table(target_table_name)
        source_table = exp.to_table(source_table_name)
        if self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=target_table.catalog or source_table.catalog,
            table_properties=self._get_table_properties(kwargs),
        ):
            self.create_table(
                target_table_name,
                self.columns(source_table_name),
                exists=exists,
                **kwargs,
            )
            return
        self.execute(
            exp.Create(
                this=target_table,
                kind="TABLE",
                exists=exists,
                properties=exp.Properties(
                    expressions=[
                        exp.LikeProperty(
                            this=source_table,
                        ),
                    ],
                ),
            )
        )

    def _build_create_comment_table_exp(
        self, table: exp.Table, table_comment: str, table_kind: str = "TABLE"
    ) -> exp.Comment | str:
        table_sql = self._to_sql(table)
        comment_sql = self._to_sql(
            exp.Literal.string(self._truncate_table_comment(table_comment.strip()))
        )
        return f"ALTER TABLE {table_sql} MODIFY COMMENT {comment_sql}"

    def _build_create_comment_column_exp(
        self, table: exp.Table, column_name: str, column_comment: str, table_kind: str = "TABLE"
    ) -> exp.Comment | str:
        table_sql = self._to_sql(table)
        col_sql = self._to_sql(exp.to_identifier(column_name, quoted=True))
        comment_sql = self._to_sql(
            exp.Literal.string(self._truncate_column_comment(column_comment.strip()))
        )
        return f"ALTER TABLE {table_sql} MODIFY COLUMN {col_sql} COMMENT {comment_sql}"

    def _truncate_table(self, table_name: TableName) -> None:
        table = t.cast(exp.Table, self._normalize_schema_tmp_identifiers(exp.to_table(table_name)))
        caught_err: t.Optional[Exception] = None
        if not self._is_iceberg_table(catalog_name=table.catalog):
            try:
                super()._truncate_table(table)
                return
            except Exception as e:
                err_msg = str(e).lower()
                if "truncate" not in err_msg and "not support" not in err_msg:
                    raise e
                caught_err = e

        # When TRUNCATE TABLE is unsupported (e.g., on Iceberg tables), clear all rows via DELETE.
        # Doris's DeleteHandler rejects DELETE without WHERE, constant WHERE TRUE, OR predicates,
        # and floating-point/complex column predicates, so we pick a key-compatible column and
        # execute two complementary `IS NOT NULL` and `IS NULL` deletes.
        cols_to_types = self.columns(table)
        if cols_to_types:
            valid_col_name = next(
                (
                    col
                    for col, dtype in cols_to_types.items()
                    if self._is_valid_olap_key_type(dtype)
                ),
                next(iter(cols_to_types)),
            )
            target_col = exp.column(valid_col_name, quoted=True)
            self.execute(exp.delete(table, where=target_col.is_(exp.null()).not_()))
            self.execute(exp.delete(table, where=target_col.is_(exp.null())))
            return
        if caught_err is not None:
            raise caught_err

    def insert_append(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        track_rows_processed: bool = True,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        query_or_df = self._native_df_to_pandas_df(query_or_df)
        target_table = exp.to_table(table_name)
        is_iceberg = self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=target_table.catalog,
            table_properties=self._get_table_properties(kwargs),
        )
        if isinstance(query_or_df, pd.DataFrame) and (
            not target_columns_to_types or not columns_to_types_all_known(target_columns_to_types)
        ):
            target_columns_to_types, source_columns = self._columns_to_types(
                query_or_df, target_columns_to_types, source_columns
            )
        elif target_columns_to_types is not None and not columns_to_types_all_known(
            target_columns_to_types
        ):
            target_columns_to_types = self.columns(table_name)

        if (
            not is_iceberg
            and target_columns_to_types
            and columns_to_types_all_known(target_columns_to_types)
        ):
            target_columns_to_types = self._reorder_olap_columns(target_columns_to_types, kwargs)
            if source_columns:
                source_col_set = set(source_columns)
                source_columns = [c for c in target_columns_to_types if c in source_col_set] + [
                    c for c in source_columns if c not in target_columns_to_types
                ]

        super().insert_append(
            table_name,
            query_or_df,
            target_columns_to_types=target_columns_to_types,
            track_rows_processed=track_rows_processed,
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
        self._ensure_not_lance_table(
            table_format=kwargs.get("table_format"),
            table_properties=self._get_table_properties(kwargs),
        )
        table = exp.to_table(table_name)
        if (
            insert_overwrite_strategy_override is None
            and where is None
            and self._is_iceberg_table(
                table_format=kwargs.get("table_format"),
                catalog_name=table.catalog,
                table_properties=self._get_table_properties(kwargs),
            )
        ):
            insert_overwrite_strategy_override = InsertOverwriteStrategy.INSERT_OVERWRITE
        return super()._insert_overwrite_by_condition(
            table_name,
            source_queries,
            target_columns_to_types=target_columns_to_types,
            where=where,
            insert_overwrite_strategy_override=insert_overwrite_strategy_override,
            **kwargs,
        )

    _TIME_UNIT_PRECEDENCE: t.ClassVar[t.Dict[str, int]] = {
        "second": 0,
        "minute": 1,
        "hour": 2,
        "day": 3,
        "week": 4,
        "month": 5,
        "quarter": 6,
        "year": 7,
    }

    @classmethod
    def _extract_time_partition_transform(
        cls, expr: exp.Expr
    ) -> t.Optional[t.Tuple[exp.Column, str]]:
        """Extract `(column, unit)` from time-truncation partition expressions (`DATE_TRUNC`,
        `TO_DATE`, `DATE`, `YEAR`/`YEARS`, `QUARTER`/`QUARTERS`, `MONTH`/`MONTHS`, `WEEK`/`WEEKS`,
        `DAY`/`DAYS`, `HOUR`/`HOURS`, `MINUTE`/`MINUTES`, `SECOND`/`SECONDS`).
        """
        curr = expr.unnest()
        col_node: t.Optional[exp.Column] = None
        raw_unit: t.Optional[str] = None

        if isinstance(curr, (exp.TimestampTrunc, exp.DateTrunc)):
            unit_arg = curr.args.get("unit")
            if isinstance(curr.this, exp.Column):
                col_node = curr.this
                raw_unit = cls._extract_str_value(unit_arg)
            elif isinstance(unit_arg, exp.Column):
                col_node = unit_arg
                raw_unit = cls._extract_str_value(curr.this)
        elif isinstance(curr, (exp.TsOrDsToDate, exp.Date)):
            if isinstance(curr.this, exp.Column):
                col_node = curr.this
                raw_unit = "day"
        elif isinstance(
            curr,
            (
                exp.Year,
                exp.Quarter,
                exp.Month,
                exp.Week,
                exp.WeekOfYear,
                exp.Day,
                exp.Hour,
                exp.Minute,
                exp.Second,
            ),
        ):
            inner_arg = curr.this.unnest() if isinstance(curr.this, exp.Expr) else None
            if isinstance(inner_arg, (exp.TsOrDsToDate, exp.Date)) and isinstance(
                inner_arg.this, exp.Expr
            ):
                inner_arg = inner_arg.this.unnest()
            if isinstance(inner_arg, exp.Column):
                col_node = inner_arg
                if isinstance(curr, exp.Year):
                    raw_unit = "year"
                elif isinstance(curr, exp.Quarter):
                    raw_unit = "quarter"
                elif isinstance(curr, exp.Month):
                    raw_unit = "month"
                elif isinstance(curr, (exp.Week, exp.WeekOfYear)):
                    raw_unit = "week"
                elif isinstance(curr, exp.Day):
                    raw_unit = "day"
                elif isinstance(curr, exp.Hour):
                    raw_unit = "hour"
                elif isinstance(curr, exp.Minute):
                    raw_unit = "minute"
                else:
                    raw_unit = "second"
        elif isinstance(curr, exp.Extract):
            extract_expr = (
                curr.expression.unnest() if isinstance(curr.expression, exp.Expr) else None
            )
            if isinstance(extract_expr, exp.Column):
                col_node = extract_expr
                raw_unit = cls._extract_str_value(curr.this)
        elif isinstance(curr, exp.Anonymous):
            fn_name = curr.name.upper()
            if fn_name == "DATE_TRUNC" and len(curr.expressions) == 2:
                arg0, arg1 = curr.expressions
                if isinstance(arg0, exp.Column):
                    col_node = arg0
                    raw_unit = cls._extract_str_value(arg1)
                elif isinstance(arg1, exp.Column):
                    col_node = arg1
                    raw_unit = cls._extract_str_value(arg0)
            elif (
                fn_name in ("TO_DATE", "DATE")
                and len(curr.expressions) == 1
                and isinstance(curr.expressions[0], exp.Column)
            ):
                col_node = curr.expressions[0]
                raw_unit = "day"
            elif (
                fn_name
                in (
                    "YEAR",
                    "YEARS",
                    "QUARTER",
                    "QUARTERS",
                    "MONTH",
                    "MONTHS",
                    "WEEK",
                    "WEEKS",
                    "WEEKOFYEAR",
                    "DAY",
                    "DAYS",
                    "HOUR",
                    "HOURS",
                    "MINUTE",
                    "MINUTES",
                    "SECOND",
                    "SECONDS",
                )
                and len(curr.expressions) == 1
                and isinstance(curr.expressions[0], exp.Column)
            ):
                col_node = curr.expressions[0]
                raw_unit = "week" if fn_name == "WEEKOFYEAR" else fn_name.lower()

        if col_node is not None and raw_unit:
            unit_clean = str(raw_unit).strip().strip("'\"").lower()
            if unit_clean.endswith("s") and unit_clean[:-1] in cls._TIME_UNIT_PRECEDENCE:
                unit_clean = unit_clean[:-1]
            if unit_clean in cls._TIME_UNIT_PRECEDENCE:
                clean_col = exp.column(
                    col_node.name, quoted=bool(getattr(col_node.this, "quoted", False))
                )
                return clean_col, unit_clean
        return None

    @staticmethod
    def _is_date_part_extract_fn(expr: exp.Expr) -> bool:
        curr = expr.unnest()
        if isinstance(
            curr,
            (
                exp.Year,
                exp.Quarter,
                exp.Month,
                exp.Week,
                exp.WeekOfYear,
                exp.Day,
                exp.Hour,
                exp.Minute,
                exp.Second,
                exp.Extract,
            ),
        ):
            return True
        if isinstance(curr, exp.Anonymous) and curr.name.upper() in (
            "YEAR",
            "YEARS",
            "QUARTER",
            "QUARTERS",
            "MONTH",
            "MONTHS",
            "WEEK",
            "WEEKS",
            "WEEKOFYEAR",
            "DAY",
            "DAYS",
            "HOUR",
            "HOURS",
            "MINUTE",
            "MINUTES",
            "SECOND",
            "SECONDS",
        ):
            return True
        return False

    def insert_overwrite_by_partition(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        partitioned_by: t.List[exp.Expr],
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        if not partitioned_by:
            self.replace_query(
                table_name,
                query_or_df,
                target_columns_to_types=target_columns_to_types,
                source_columns=source_columns,
                **kwargs,
            )
            return
        parsed_exprs, _ = self._parse_partition_expressions(partitioned_by)
        col_order: t.List[str] = []
        col_to_expr: t.Dict[str, exp.Column] = {}
        col_to_unit: t.Dict[str, t.Optional[str]] = {}

        for expr in parsed_exprs or partitioned_by:
            if isinstance(expr, exp.Column):
                col_lower = expr.name.lower()
                if col_lower not in col_to_expr:
                    col_order.append(col_lower)
                col_to_expr[col_lower] = exp.column(
                    expr.name, quoted=bool(getattr(expr.this, "quoted", False))
                )
                col_to_unit[col_lower] = None
            elif isinstance(expr, exp.Identifier):
                col_lower = expr.name.lower()
                if col_lower not in col_to_expr:
                    col_order.append(col_lower)
                col_to_expr[col_lower] = exp.column(expr.name, quoted=bool(expr.quoted))
                col_to_unit[col_lower] = None
            elif (transform := self._extract_time_partition_transform(expr)) is not None:
                col_expr, unit = transform
                col_lower = col_expr.name.lower()
                if col_lower not in col_to_expr:
                    col_order.append(col_lower)
                    col_to_expr[col_lower] = col_expr
                    col_to_unit[col_lower] = unit
                elif col_to_unit[col_lower] is not None:
                    prev_unit = col_to_unit[col_lower]
                    assert prev_unit is not None
                    if self._TIME_UNIT_PRECEDENCE[unit] < self._TIME_UNIT_PRECEDENCE[prev_unit]:
                        col_to_unit[col_lower] = unit
            else:
                raise SQLMeshError(
                    f"Doris insert_overwrite_by_partition does not support partition expression '{expr.sql(dialect=self.dialect)}'. "
                    "Only column names and time-truncation transforms (DATE_TRUNC, TO_DATE, DATE, YEAR, MONTH, DAY, HOUR) are supported."
                )

        key_exprs: t.List[exp.Expr] = []
        for col_lower in col_order:
            col_expr = col_to_expr[col_lower]
            col_unit = col_to_unit[col_lower]
            if col_unit is None:
                key_exprs.append(col_expr)
            else:
                key_exprs.append(
                    exp.Anonymous(
                        this="DATE_TRUNC",
                        expressions=[col_expr, exp.Literal.string(col_unit)],
                    )
                )

        target_table = exp.to_table(table_name)
        is_iceberg = self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=target_table.catalog,
            table_properties=self._get_table_properties(kwargs),
        )
        if (
            target_columns_to_types
            and columns_to_types_all_known(target_columns_to_types)
            and not is_iceberg
        ):
            reorder_kwargs = dict(kwargs)
            reorder_kwargs["partitioned_by"] = partitioned_by
            target_columns_to_types = self._reorder_olap_columns(
                target_columns_to_types, reorder_kwargs
            )
            if source_columns:
                source_col_set = set(source_columns)
                source_columns = [c for c in target_columns_to_types if c in source_col_set] + [
                    c for c in source_columns if c not in target_columns_to_types
                ]

        if self.INSERT_OVERWRITE_STRATEGY.is_insert_overwrite:
            super().insert_overwrite_by_partition(
                table_name,
                query_or_df,
                key_exprs or partitioned_by,
                target_columns_to_types=target_columns_to_types,
                source_columns=source_columns,
            )
        else:
            self._replace_by_key(
                table_name,
                query_or_df,
                target_columns_to_types,
                key_exprs or partitioned_by,
                is_unique_key=False,
                source_columns=source_columns,
                **kwargs,
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
        target_table = exp.to_table(table_name)
        is_iceberg = self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=target_table.catalog,
            table_properties=self._get_table_properties(kwargs),
        )
        if (
            not is_iceberg
            and target_columns_to_types
            and columns_to_types_all_known(target_columns_to_types)
        ):
            target_columns_to_types = self._reorder_olap_columns(target_columns_to_types, kwargs)
            if source_columns:
                source_col_set = set(source_columns)
                source_columns = [c for c in target_columns_to_types if c in source_col_set] + [
                    c for c in source_columns if c not in target_columns_to_types
                ]
        return super().insert_overwrite_by_time_partition(
            table_name,
            query_or_df,
            start=start,
            end=end,
            time_formatter=time_formatter,
            time_column=time_column,
            target_columns_to_types=target_columns_to_types,
            source_columns=source_columns,
            **kwargs,
        )

    def delete_from(
        self, table_name: TableName, where: t.Optional[t.Union[str, exp.Expr]] = None
    ) -> None:
        """Delete from a table in Doris."""
        if not where:
            self._truncate_table(table_name)
            return

        if isinstance(where, str):
            where = parse_one(where, read=self.dialect)
        else:
            where = where.copy()

        where = self._simplify_boolean_where(where)
        if where == exp.true():
            self._truncate_table(table_name)
            return
        if where == exp.false():
            return

        where = self._unwrap_literal_casts_in_where(where)
        where = self._pushdown_not_in_where(where)
        where = self._rewrite_between_in_where(where)
        where = self._rewrite_direct_time_transforms_in_where(where)
        where = self._simplify_boolean_where(where)
        if where == exp.true():
            self._truncate_table(table_name)
            return
        if where == exp.false():
            return
        where = self._normalize_boolean_column_conjuncts(where)

        subquery_info = self._extract_subquery_from_where(where)
        if subquery_info is not None:
            self._execute_delete_with_subquery(table_name, *subquery_info)
            return

        tuple_in_info = self._extract_literal_tuple_in_from_where(where)
        if tuple_in_info is not None:
            self._delete_key_rows(table_name, *tuple_in_info, is_ast_values=True)
            return

        or_key_info = self._extract_or_key_rows_from_where(where)
        if or_key_info is not None:
            self._delete_key_rows(table_name, *or_key_info, is_ast_values=True)
            return

        expanded_or_sets = self._expand_or_free_extra_condition_sets([where])
        if len(expanded_or_sets) > 1:
            for branch_conjuncts in expanded_or_sets:
                if branch_conjuncts:
                    self.delete_from(table_name, exp.and_(*branch_conjuncts, copy=False))
            return

        target_table = exp.to_table(table_name)
        where = where.unnest()
        for col in where.find_all(exp.Column):
            if not col.find_ancestor(exp.Subquery, exp.Select) and (
                col.table and col.table.lower() == target_table.name.lower()
            ):
                col.set("table", None)
                col.set("db", None)
                col.set("catalog", None)
        super().delete_from(table_name, where)

    @classmethod
    def _simplify_boolean_where(cls, expr: exp.Expr) -> exp.Expr:
        if isinstance(expr, (exp.Subquery, exp.Select)):
            return expr
        for k, v in list(expr.args.items()):
            if isinstance(v, exp.Expr):
                expr.set(k, cls._simplify_boolean_where(v))
            elif isinstance(v, list):
                expr.set(
                    k,
                    [
                        cls._simplify_boolean_where(item) if isinstance(item, exp.Expr) else item
                        for item in v
                    ],
                )
        if isinstance(expr, exp.Paren) and isinstance(expr.this, exp.Boolean):
            return expr.this
        if (
            isinstance(expr, exp.EQ)
            and isinstance(expr.this, exp.Literal)
            and isinstance(expr.expression, exp.Literal)
            and expr.this.is_number == expr.expression.is_number
        ):
            return exp.true() if expr.this == expr.expression else exp.false()
        if (
            isinstance(expr, exp.NEQ)
            and isinstance(expr.this, exp.Literal)
            and isinstance(expr.expression, exp.Literal)
            and expr.this.is_number == expr.expression.is_number
        ):
            return exp.false() if expr.this == expr.expression else exp.true()
        if isinstance(expr, exp.Not) and isinstance(expr.this, exp.Boolean):
            return exp.false() if expr.this == exp.true() else exp.true()
        if isinstance(expr, exp.And):
            if expr.this == exp.false() or expr.expression == exp.false():
                return exp.false()
            if expr.this == exp.true():
                return expr.expression
            if expr.expression == exp.true():
                return expr.this
        if isinstance(expr, exp.Or):
            if expr.this == exp.true() or expr.expression == exp.true():
                return exp.true()
            if expr.this == exp.false():
                return expr.expression
            if expr.expression == exp.false():
                return expr.this
        return expr

    @classmethod
    def _flatten_and_conjuncts(cls, expr: exp.Expr) -> t.List[exp.Expr]:
        unwrapped = expr.unnest()
        if isinstance(unwrapped, exp.And):
            result: t.List[exp.Expr] = []
            for child in unwrapped.flatten():
                result.extend(cls._flatten_and_conjuncts(child))
            return result
        return [unwrapped]

    @classmethod
    def _flatten_or_disjuncts(cls, expr: exp.Expr) -> t.List[exp.Expr]:
        unwrapped = expr.unnest()
        if isinstance(unwrapped, exp.Or):
            result: t.List[exp.Expr] = []
            for child in unwrapped.flatten():
                result.extend(cls._flatten_or_disjuncts(child))
            return result
        return [unwrapped]

    def _normalize_extra_conditions(self, extra_conditions: t.List[exp.Expr]) -> t.List[exp.Expr]:
        normalized: t.List[exp.Expr] = []
        for raw_cond in extra_conditions:
            for cond in self._flatten_and_conjuncts(raw_cond):
                unwrapped = cond.unnest()
                if isinstance(unwrapped, exp.Or):
                    or_info = self._extract_or_key_rows_from_where(unwrapped)
                    if (
                        or_info is not None
                        and len(or_info[0]) == 1
                        and not or_info[2]
                        and all(not isinstance(r[0], exp.Null) for r in or_info[1])
                    ):
                        col_expr = exp.column(or_info[0][0].name, quoted=True)
                        vals = [r[0].copy() for r in or_info[1]]
                        normalized.append(col_expr.isin(*vals))
                        continue
                normalized.append(cond)
        return normalized

    @classmethod
    def _pushdown_not_in_where(cls, expr: exp.Expr) -> exp.Expr:
        if isinstance(expr, (exp.Subquery, exp.Select)):
            return expr
        if isinstance(expr, exp.Not) and isinstance(expr.this, exp.Expr):
            inner = expr.this.unnest()
            if isinstance(inner, exp.Not) and isinstance(inner.this, exp.Expr):
                return cls._pushdown_not_in_where(inner.this.unnest().copy())
            if isinstance(inner, exp.And):
                return exp.Paren(
                    this=exp.or_(
                        cls._pushdown_not_in_where(exp.not_(inner.this.copy(), copy=False)),
                        cls._pushdown_not_in_where(exp.not_(inner.expression.copy(), copy=False)),
                        copy=False,
                    )
                )
            if isinstance(inner, exp.Or):
                return exp.Paren(
                    this=exp.and_(
                        cls._pushdown_not_in_where(exp.not_(inner.this.copy(), copy=False)),
                        cls._pushdown_not_in_where(exp.not_(inner.expression.copy(), copy=False)),
                        copy=False,
                    )
                )
            if isinstance(inner, exp.Between):
                return exp.Paren(
                    this=exp.or_(
                        exp.LT(this=inner.this.copy(), expression=inner.args["low"].copy()),
                        exp.GT(this=inner.this.copy(), expression=inner.args["high"].copy()),
                        copy=False,
                    )
                )
            if isinstance(inner, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                neg_map: t.Dict[t.Type[exp.Binary], t.Type[exp.Binary]] = {
                    exp.EQ: exp.NEQ,
                    exp.NEQ: exp.EQ,
                    exp.GT: exp.LTE,
                    exp.GTE: exp.LT,
                    exp.LT: exp.GTE,
                    exp.LTE: exp.GT,
                }
                neg_cls = neg_map[type(inner)]
                return neg_cls(this=inner.this.copy(), expression=inner.expression.copy())
            if isinstance(inner, (exp.In, exp.Is)) and expr.this is not inner:
                return exp.Not(this=inner.copy())
        for k, v in list(expr.args.items()):
            if isinstance(v, exp.Expr):
                expr.set(k, cls._pushdown_not_in_where(v))
            elif isinstance(v, list):
                expr.set(
                    k,
                    [
                        cls._pushdown_not_in_where(item) if isinstance(item, exp.Expr) else item
                        for item in v
                    ],
                )
        return expr

    def _expand_or_to_conjunct_sets(self, expr: exp.Expr) -> t.List[t.List[exp.Expr]]:
        expr = self._pushdown_not_in_where(expr.copy())
        conjuncts = self._flatten_and_conjuncts(expr)
        if len(conjuncts) > 1:
            sets: t.List[t.List[exp.Expr]] = [[]]
            for c in conjuncts:
                sub_sets = self._expand_or_to_conjunct_sets(c)
                sets = [[*s, *b] for s in sets for b in sub_sets]
            return sets
        single = conjuncts[0]
        disjuncts = self._flatten_or_disjuncts(single)
        if len(disjuncts) > 1:
            or_sets: t.List[t.List[exp.Expr]] = []
            for d in disjuncts:
                or_sets.extend(self._expand_or_to_conjunct_sets(d))
            return or_sets
        leaf = disjuncts[0]
        norm = self._normalize_single_boolean_conjunct(leaf)
        return [[norm if norm is not None else leaf.copy()]]

    def _expand_or_free_extra_condition_sets(
        self, extra_conditions: t.Sequence[exp.Expr]
    ) -> t.List[t.List[exp.Expr]]:
        condition_sets: t.List[t.List[exp.Expr]] = [[]]
        for cond in extra_conditions:
            sub_sets = self._expand_or_to_conjunct_sets(cond)
            condition_sets = [[*cset, *branch] for cset in condition_sets for branch in sub_sets]
        return condition_sets

    def _compute_time_transform_bounds(
        self,
        t_expr: exp.Expr,
        unit: str,
        val_expr: exp.Expr,
    ) -> t.Optional[t.Tuple[exp.Literal, exp.Literal, bool]]:
        if self._is_date_part_extract_fn(t_expr) and unit != "year":
            raise SQLMeshError(
                f"Doris delete_from cannot rewrite non-year date-part predicate '{t_expr.sql(dialect=self.dialect)}' "
                "into a contiguous range. Use DATE_TRUNC, TO_DATE, DATE, or YEAR instead."
            )
        raw_val = self._extract_str_value(val_expr)
        if raw_val is None:
            return None
        raw_str = raw_val.strip()
        if self._is_date_part_extract_fn(t_expr) and unit == "year":
            try:
                yr_int = (
                    int(raw_str[:4])
                    if len(raw_str) >= 4 and raw_str[:4].isdigit()
                    else int(raw_str)
                )
            except ValueError:
                return None
            return (
                exp.Literal.string(f"{yr_int:04d}-01-01"),
                exp.Literal.string(f"{yr_int + 1:04d}-01-01"),
                True,
            )
        try:
            dt = datetime.datetime.fromisoformat(raw_str)
        except ValueError:
            return None
        has_time = " " in raw_str or "T" in raw_str or unit in ("hour", "minute", "second")
        if unit == "year":
            low_dt = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            high_dt = low_dt.replace(year=low_dt.year + 1)
        elif unit == "quarter":
            q_month = ((dt.month - 1) // 3) * 3 + 1
            low_dt = dt.replace(month=q_month, day=1, hour=0, minute=0, second=0, microsecond=0)
            high_dt = low_dt.replace(
                year=low_dt.year + (1 if q_month == 10 else 0),
                month=(1 if q_month == 10 else q_month + 3),
            )
        elif unit == "month":
            low_dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            high_dt = low_dt.replace(
                year=low_dt.year + (1 if low_dt.month == 12 else 0),
                month=(1 if low_dt.month == 12 else low_dt.month + 1),
            )
        elif unit == "week":
            low_dt = (dt - datetime.timedelta(days=dt.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            high_dt = low_dt + datetime.timedelta(days=7)
        elif unit == "day":
            low_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            high_dt = low_dt + datetime.timedelta(days=1)
        elif unit == "hour":
            low_dt = dt.replace(minute=0, second=0, microsecond=0)
            high_dt = low_dt + datetime.timedelta(hours=1)
        elif unit == "minute":
            low_dt = dt.replace(second=0, microsecond=0)
            high_dt = low_dt + datetime.timedelta(minutes=1)
        elif unit == "second":
            low_dt = dt.replace(microsecond=0)
            high_dt = low_dt + datetime.timedelta(seconds=1)
        else:
            return None
        fmt = "%Y-%m-%d %H:%M:%S" if has_time else "%Y-%m-%d"
        return (
            exp.Literal.string(low_dt.strftime(fmt)),
            exp.Literal.string(high_dt.strftime(fmt)),
            dt == low_dt,
        )

    def _build_time_transform_range(
        self,
        t_expr: exp.Expr,
        col_expr: exp.Column,
        unit: str,
        val_expr: exp.Expr,
    ) -> t.Optional[exp.Expr]:
        bounds = self._compute_time_transform_bounds(t_expr, unit, val_expr)
        if bounds is None:
            return None
        low_lit, high_lit, _ = bounds
        return exp.and_(
            exp.GTE(
                this=col_expr.copy(),
                expression=low_lit,
            ),
            exp.LT(
                this=col_expr.copy(),
                expression=high_lit,
            ),
            copy=False,
        )

    def _rewrite_direct_time_transforms_in_where(self, where: exp.Expr) -> exp.Expr:
        for node in list(
            where.find_all(
                exp.EQ,
                exp.NEQ,
                exp.GTE,
                exp.GT,
                exp.LTE,
                exp.LT,
                exp.In,
                exp.Is,
            )
        ):
            if node.find_ancestor(exp.Subquery, exp.Select):
                continue
            if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
                is_trans = self._extract_time_partition_transform(node.this)
                if is_trans is not None:
                    node.set("this", is_trans[0].copy())
                continue
            parent_not = node.parent if isinstance(node.parent, exp.Not) else None
            if node.find_ancestor(exp.Not) and parent_not is None:
                continue
            target_node: exp.Expr = parent_not if parent_not is not None else node
            if isinstance(node, (exp.EQ, exp.NEQ, exp.GTE, exp.GT, exp.LTE, exp.LT)):
                left_trans = self._extract_time_partition_transform(node.this)
                right_trans = self._extract_time_partition_transform(node.expression)
                t_expr: t.Optional[exp.Expr] = None
                col_expr: t.Optional[exp.Column] = None
                unit: t.Optional[str] = None
                val_expr: t.Optional[exp.Expr] = None
                transform_on_left = True
                if left_trans is not None and self._is_literal_key_value(node.expression):
                    t_expr = node.this
                    col_expr, unit = left_trans
                    val_expr = node.expression
                    transform_on_left = True
                elif right_trans is not None and self._is_literal_key_value(node.this):
                    t_expr = node.expression
                    col_expr, unit = right_trans
                    val_expr = node.this
                    transform_on_left = False
                if (
                    t_expr is not None
                    and col_expr is not None
                    and unit is not None
                    and val_expr is not None
                ):
                    bounds = self._compute_time_transform_bounds(t_expr, unit, val_expr)
                    if bounds is not None:
                        low_lit, high_lit, is_aligned = bounds
                        effective_op: t.Type[exp.Binary] = type(node)
                        if not transform_on_left:
                            flip_map: t.Dict[t.Type[exp.Binary], t.Type[exp.Binary]] = {
                                exp.GTE: exp.LTE,
                                exp.GT: exp.LT,
                                exp.LTE: exp.GTE,
                                exp.LT: exp.GT,
                            }
                            effective_op = flip_map.get(effective_op, effective_op)
                        if parent_not is not None:
                            neg_map: t.Dict[t.Type[exp.Binary], t.Type[exp.Binary]] = {
                                exp.EQ: exp.NEQ,
                                exp.NEQ: exp.EQ,
                                exp.GTE: exp.LT,
                                exp.GT: exp.LTE,
                                exp.LTE: exp.GT,
                                exp.LT: exp.GTE,
                            }
                            effective_op = neg_map.get(effective_op, effective_op)
                        replacement: t.Optional[exp.Expr] = None
                        if effective_op is exp.EQ:
                            if is_aligned:
                                replacement = exp.and_(
                                    exp.GTE(this=col_expr.copy(), expression=low_lit.copy()),
                                    exp.LT(this=col_expr.copy(), expression=high_lit.copy()),
                                    copy=False,
                                )
                            else:
                                replacement = exp.false()
                        elif effective_op is exp.NEQ:
                            if is_aligned:
                                replacement = exp.or_(
                                    exp.LT(this=col_expr.copy(), expression=low_lit.copy()),
                                    exp.GTE(this=col_expr.copy(), expression=high_lit.copy()),
                                    copy=False,
                                )
                            else:
                                replacement = exp.not_(
                                    exp.Is(this=col_expr.copy(), expression=exp.Null())
                                )
                        elif effective_op is exp.GTE:
                            bound_lit = low_lit if is_aligned else high_lit
                            replacement = exp.GTE(this=col_expr.copy(), expression=bound_lit.copy())
                        elif effective_op is exp.GT:
                            replacement = exp.GTE(this=col_expr.copy(), expression=high_lit.copy())
                        elif effective_op is exp.LTE:
                            replacement = exp.LT(this=col_expr.copy(), expression=high_lit.copy())
                        elif effective_op is exp.LT:
                            bound_lit = low_lit if is_aligned else high_lit
                            replacement = exp.LT(this=col_expr.copy(), expression=bound_lit.copy())
                        if replacement is not None:
                            if target_node is where:
                                where = replacement
                            else:
                                if isinstance(replacement, (exp.And, exp.Or)) and not isinstance(
                                    target_node.parent, (exp.Where, exp.Paren)
                                ):
                                    replacement = exp.Paren(this=replacement)
                                target_node.replace(replacement)
            elif (
                isinstance(node, exp.In)
                and self._get_in_subquery(node) is None
                and node.expressions
            ):
                in_trans = self._extract_time_partition_transform(node.this)
                if in_trans is not None and all(
                    self._is_literal_key_value(v) for v in node.expressions
                ):
                    col_expr, unit = in_trans
                    bounds_list = [
                        self._compute_time_transform_bounds(node.this, unit, v)
                        for v in node.expressions
                    ]
                    if bounds_list and all(b is not None for b in bounds_list):
                        aligned_ranges = [
                            exp.and_(
                                exp.GTE(this=col_expr.copy(), expression=b[0].copy()),
                                exp.LT(this=col_expr.copy(), expression=b[1].copy()),
                                copy=False,
                            )
                            for b in bounds_list
                            if b is not None and b[2]
                        ]
                        if parent_not is not None:
                            if not aligned_ranges:
                                replacement_in: exp.Expr = exp.not_(
                                    exp.Is(this=col_expr.copy(), expression=exp.Null())
                                )
                            else:
                                not_in_parts: t.List[exp.Expr] = [
                                    exp.Paren(
                                        this=exp.or_(
                                            exp.LT(
                                                this=col_expr.copy(),
                                                expression=r.this.expression.copy(),
                                            ),
                                            exp.GTE(
                                                this=col_expr.copy(),
                                                expression=r.expression.expression.copy(),
                                            ),
                                            copy=False,
                                        )
                                    )
                                    for r in aligned_ranges
                                ]
                                replacement_in = exp.and_(*not_in_parts, copy=False)
                        else:
                            if not aligned_ranges:
                                replacement_in = exp.false()
                            elif len(aligned_ranges) == 1:
                                replacement_in = aligned_ranges[0]
                            else:
                                replacement_in = exp.or_(
                                    *[exp.Paren(this=r) for r in aligned_ranges],
                                    copy=False,
                                )
                        if target_node is where:
                            where = replacement_in
                        else:
                            if isinstance(replacement_in, (exp.And, exp.Or)) and not isinstance(
                                target_node.parent, (exp.Where, exp.Paren)
                            ):
                                replacement_in = exp.Paren(this=replacement_in)
                            target_node.replace(replacement_in)
        return where

    @classmethod
    def _unwrap_literal_casts_in_where(cls, where: exp.Expr) -> exp.Expr:
        """Unwrap top-level `CAST(<literal> AS ...)` expressions in `DELETE WHERE` predicates
        (e.g. from `to_time_column` in `insert_overwrite_by_time_partition`) because Doris's OLAP
        `DeleteHandler` only accepts plain literals on the RHS of comparison and `IN` predicates.
        """
        for cast_node in list(where.find_all(exp.Cast, exp.TryCast)):
            if cast_node.find_ancestor(exp.Subquery, exp.Select):
                continue
            inner = cast_node.this
            while isinstance(inner, (exp.Cast, exp.TryCast)):
                inner = inner.this
            if inner is not None and cls._is_literal_key_value(inner):
                cast_node.replace(inner.copy())
        return where

    @staticmethod
    def _rewrite_between_in_where(where: exp.Expr) -> exp.Expr:
        if isinstance(where, exp.Between):
            return exp.and_(
                exp.GTE(this=where.this.copy(), expression=where.args["low"].copy()),
                exp.LTE(this=where.this.copy(), expression=where.args["high"].copy()),
                copy=False,
            )
        for between in list(where.find_all(exp.Between)):
            if between.find_ancestor(exp.Subquery, exp.Select, exp.Not):
                continue
            replacement: exp.Expr = exp.and_(
                exp.GTE(this=between.this.copy(), expression=between.args["low"].copy()),
                exp.LTE(this=between.this.copy(), expression=between.args["high"].copy()),
                copy=False,
            )
            if not isinstance(between.parent, (exp.Where, exp.Paren)):
                replacement = exp.Paren(this=replacement)
            between.replace(replacement)
        return where

    def _extract_subquery_from_where(
        self, where: exp.Expr
    ) -> t.Optional[t.Tuple[t.List[exp.Expr], exp.Subquery, t.List[exp.Expr]]]:
        """Extract positive IN (SELECT ...) subquery and any sibling AND conditions."""
        conjuncts = self._flatten_and_conjuncts(where)

        for cand_idx, cond in enumerate(conjuncts):
            unwrapped_cond = cond.unnest()
            if not isinstance(unwrapped_cond, exp.In):
                continue
            subquery = self._get_in_subquery(unwrapped_cond)
            if subquery is None:
                continue

            lhs = unwrapped_cond.this
            select_expr = subquery.this
            key_exprs: t.List[exp.Expr] = []

            lhs_concat = self._unwrap_concat_ws(lhs)
            if lhs_concat is not None:
                unwrapped_select_expr = self._unwrap_concat_ws_in_query(
                    select_expr, len(lhs_concat.expressions)
                )
                if unwrapped_select_expr is None:
                    continue
                key_exprs = [e.copy() for e in lhs_concat.expressions[1:]]
                subquery = exp.Subquery(this=unwrapped_select_expr)
            elif isinstance(lhs, exp.Tuple):
                key_exprs = [e.copy() for e in lhs.expressions]
            else:
                key_exprs = [lhs.copy()]

            normalized_keys: t.List[exp.Expr] = []
            valid_keys = True
            for k in key_exprs:
                if isinstance(k, exp.Column):
                    normalized_keys.append(k)
                elif isinstance(k, exp.Identifier):
                    normalized_keys.append(exp.Column(this=k.copy()))
                elif self._extract_time_partition_transform(k) is not None:
                    normalized_keys.append(k.copy())
                else:
                    valid_keys = False
                    break

            if not valid_keys or not normalized_keys:
                continue

            extra_conditions = [c for i, c in enumerate(conjuncts) if i != cand_idx]
            return normalized_keys, subquery, self._normalize_extra_conditions(extra_conditions)

        return None

    def _extract_literal_tuple_in_from_where(
        self, where: exp.Expr
    ) -> t.Optional[t.Tuple[t.List[exp.Expr], t.List[t.Tuple[exp.Expr, ...]], t.List[exp.Expr]]]:
        """Extract positive literal tuple IN predicate `(a, b) IN ((1, 2), (3, 4))` and sibling AND conditions."""
        conjuncts = self._flatten_and_conjuncts(where)

        for cand_idx, cond in enumerate(conjuncts):
            unwrapped_cond = cond.unnest()
            if not (
                isinstance(unwrapped_cond, exp.In)
                and self._get_in_subquery(unwrapped_cond) is None
                and isinstance(unwrapped_cond.this, exp.Tuple)
                and len(unwrapped_cond.this.expressions) >= 2
                and unwrapped_cond.expressions
                and all(
                    isinstance(item, exp.Tuple)
                    and len(item.expressions) == len(unwrapped_cond.this.expressions)
                    for item in unwrapped_cond.expressions
                )
            ):
                continue

            normalized_keys: t.List[exp.Expr] = []
            valid_keys = True
            for k in unwrapped_cond.this.expressions:
                if isinstance(k, exp.Column):
                    normalized_keys.append(k.copy())
                elif isinstance(k, exp.Identifier):
                    normalized_keys.append(exp.Column(this=k.copy()))
                else:
                    valid_keys = False
                    break

            if not valid_keys or not normalized_keys:
                continue

            tuple_rows: t.List[t.Tuple[exp.Expr, ...]] = [
                tuple(e.copy() for e in item.expressions)
                for item in unwrapped_cond.expressions
                if isinstance(item, exp.Tuple)
            ]
            if not tuple_rows:
                continue

            extra_conditions = [c for i, c in enumerate(conjuncts) if i != cand_idx]
            return normalized_keys, tuple_rows, self._normalize_extra_conditions(extra_conditions)

        return None

    @staticmethod
    def _is_literal_key_value(expr: exp.Expr) -> bool:
        return isinstance(expr, (exp.Literal, exp.Boolean, exp.Null)) or (
            isinstance(expr, exp.Neg) and isinstance(expr.this, exp.Literal)
        )

    @classmethod
    def _normalize_single_boolean_conjunct(cls, cond: exp.Expr) -> t.Optional[exp.Expr]:
        inner = cond.unnest()
        if isinstance(inner, exp.Column):
            return inner.copy().eq(exp.true())
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Boolean):
            return inner.this.copy().eq(inner.expression.copy())
        if isinstance(inner, exp.Not):
            neg_inner = inner.this.unnest()
            if isinstance(neg_inner, exp.Column):
                return neg_inner.copy().eq(exp.false())
            if isinstance(neg_inner, exp.Is) and isinstance(neg_inner.expression, exp.Boolean):
                return neg_inner.this.copy().eq(
                    exp.Boolean(this=not bool(neg_inner.expression.this))
                )
        return None

    @classmethod
    def _normalize_boolean_column_conjuncts(cls, where: exp.Expr) -> exp.Expr:
        """Rewrite top-level bare boolean column conjuncts (`AND is_dev`, `AND NOT is_dev`) and
        `IS [NOT] TRUE/FALSE` predicates to explicit binary comparisons (`is_dev = TRUE`,
        `is_dev = FALSE`) required by Doris DeleteHandler.
        """
        single = cls._normalize_single_boolean_conjunct(where)
        if single is not None:
            return single
        unwrapped = where.unnest()
        if isinstance(unwrapped, exp.And):
            conjuncts = cls._flatten_and_conjuncts(unwrapped)
            changed = len(conjuncts) != len(list(unwrapped.flatten()))
            new_conjuncts: t.List[exp.Expr] = []
            for cond in conjuncts:
                normalized = cls._normalize_single_boolean_conjunct(cond)
                if normalized is not None:
                    new_conjuncts.append(normalized)
                    changed = True
                else:
                    new_conjuncts.append(cond)
            if changed:
                return exp.and_(*new_conjuncts, copy=False)
        return where

    def _extract_or_key_rows_from_where(
        self, where: exp.Expr
    ) -> t.Optional[t.Tuple[t.List[exp.Expr], t.List[t.Tuple[exp.Expr, ...]], t.List[exp.Expr]]]:
        """Extract a top-level OR of equality/IS NULL key predicates (e.g. from `snapshot_id_filter`
        when `SUPPORTS_TUPLE_IN = False`, or `delete_expired_environments`) and sibling AND conditions.
        """
        conjuncts = self._flatten_and_conjuncts(where)

        for cand_idx, cond in enumerate(conjuncts):
            unwrapped_cond = cond.unnest()
            if not isinstance(unwrapped_cond, exp.Or):
                continue

            disjuncts = self._flatten_or_disjuncts(unwrapped_cond)
            if len(disjuncts) < 2:
                continue

            canonical_col_order: t.Optional[t.List[str]] = None
            canonical_cols: t.List[exp.Expr] = []
            tuple_rows: t.List[t.Tuple[exp.Expr, ...]] = []
            valid_or = True

            for branch in disjuncts:
                branch_conjuncts = self._flatten_and_conjuncts(branch)
                col_to_val: t.Dict[str, exp.Expr] = {}
                col_to_expr: t.Dict[str, exp.Expr] = {}
                for item in branch_conjuncts:
                    normalized_bool = self._normalize_single_boolean_conjunct(item)
                    item_unwrapped = (
                        normalized_bool if normalized_bool is not None else item.unnest()
                    )
                    if isinstance(item_unwrapped, exp.EQ):
                        left, right = item_unwrapped.this, item_unwrapped.expression
                        if isinstance(
                            left, (exp.Column, exp.Identifier)
                        ) and self._is_literal_key_value(right):
                            col_expr = (
                                left.copy()
                                if isinstance(left, exp.Column)
                                else exp.Column(this=left.copy())
                            )
                            val_expr = right.copy()
                        elif isinstance(
                            right, (exp.Column, exp.Identifier)
                        ) and self._is_literal_key_value(left):
                            col_expr = (
                                right.copy()
                                if isinstance(right, exp.Column)
                                else exp.Column(this=right.copy())
                            )
                            val_expr = left.copy()
                        else:
                            valid_or = False
                            break
                    elif isinstance(item_unwrapped, exp.Is) and isinstance(
                        item_unwrapped.expression, exp.Null
                    ):
                        left = item_unwrapped.this
                        if isinstance(left, (exp.Column, exp.Identifier)):
                            col_expr = (
                                left.copy()
                                if isinstance(left, exp.Column)
                                else exp.Column(this=left.copy())
                            )
                            val_expr = exp.null()
                        else:
                            valid_or = False
                            break
                    else:
                        valid_or = False
                        break

                    col_key = col_expr.name.lower()
                    if not col_key or col_key in col_to_val:
                        valid_or = False
                        break
                    col_to_val[col_key] = val_expr
                    col_to_expr[col_key] = col_expr

                if not valid_or or not col_to_val:
                    valid_or = False
                    break

                if canonical_col_order is None:
                    canonical_col_order = list(col_to_val.keys())
                    canonical_cols = [col_to_expr[k] for k in canonical_col_order]
                elif set(col_to_val.keys()) != set(canonical_col_order):
                    valid_or = False
                    break

                tuple_rows.append(tuple(col_to_val[k] for k in canonical_col_order))

            if valid_or and canonical_cols and tuple_rows:
                extra_conditions = [c for i, c in enumerate(conjuncts) if i != cand_idx]
                return (
                    canonical_cols,
                    tuple_rows,
                    self._normalize_extra_conditions(extra_conditions),
                )

        return None

    @staticmethod
    def _unwrap_concat_ws(expr: exp.Expr) -> t.Optional[exp.Expr]:
        curr = expr.unnest()
        if isinstance(curr, exp.Column) and isinstance(curr.this, exp.Expr):
            curr = curr.this.unnest()
        if isinstance(curr, exp.ConcatWs) or (
            isinstance(curr, exp.Anonymous) and curr.name.upper() == "CONCAT_WS"
        ):
            return curr
        return None

    @classmethod
    def _unwrap_concat_ws_in_query(
        cls, query_expr: exp.Expr, expected_arity: int
    ) -> t.Optional[exp.Expr]:
        if isinstance(query_expr, exp.Subquery):
            inner = cls._unwrap_concat_ws_in_query(query_expr.this, expected_arity)
            if inner is None:
                return None
            unwrapped_sq = query_expr.copy()
            unwrapped_sq.set("this", inner)
            return unwrapped_sq
        if isinstance(query_expr, exp.SetOperation):
            left = cls._unwrap_concat_ws_in_query(query_expr.this, expected_arity)
            right = cls._unwrap_concat_ws_in_query(query_expr.expression, expected_arity)
            if left is None or right is None:
                return None
            unwrapped_set = query_expr.copy()
            unwrapped_set.set("this", left)
            unwrapped_set.set("expression", right)
            return unwrapped_set
        if isinstance(query_expr, exp.Select) and len(query_expr.expressions) == 1:
            first_sel = query_expr.expressions[0]
            raw_sel = first_sel.this if isinstance(first_sel, exp.Alias) else first_sel
            sel_concat = cls._unwrap_concat_ws(raw_sel)
            if sel_concat is None or len(sel_concat.expressions) != expected_arity:
                return None
            unwrapped_sel = query_expr.copy()
            unwrapped_sel.set("expressions", [e.copy() for e in sel_concat.expressions[1:]])
            return unwrapped_sel
        return None

    @staticmethod
    def _get_in_subquery(in_expr: exp.In) -> t.Optional[exp.Subquery]:
        query = in_expr.args.get("query")
        if isinstance(query, exp.Subquery):
            return query.copy()
        if isinstance(query, (exp.Select, exp.SetOperation)):
            return exp.Subquery(this=query.copy())
        if len(in_expr.expressions) == 1:
            first = in_expr.expressions[0]
            if isinstance(first, exp.Subquery):
                return first.copy()
            if isinstance(first, (exp.Select, exp.SetOperation)):
                return exp.Subquery(this=first.copy())
        return None

    def _execute_delete_with_subquery(
        self,
        table_name: TableName,
        key_exprs: t.List[exp.Expr],
        subquery: exp.Subquery,
        extra_conditions: t.List[exp.Expr],
    ) -> None:
        """Execute DELETE FROM ... USING (SELECT ...) in Doris, with literal batch fallback for DUPLICATE KEY/Iceberg."""
        if any(self._extract_time_partition_transform(k) is not None for k in key_exprs):
            self._delete_by_time_transform_keys(table_name, key_exprs, subquery, extra_conditions)
            return

        target_table = exp.to_table(table_name)
        if self._is_iceberg_table(catalog_name=target_table.catalog) or getattr(
            self, "_in_iceberg_replace_by_key", False
        ):
            self._delete_by_literal_keys(table_name, key_exprs, subquery, extra_conditions)
            return

        join_conditions: t.List[exp.Expr] = []
        orig_subquery = subquery.copy()
        subquery = subquery.copy()
        subq_branches: t.List[exp.Select] = []

        def _collect_subq_branches(node: t.Optional[exp.Expr]) -> None:
            if isinstance(node, exp.Subquery):
                _collect_subq_branches(node.this)
            elif isinstance(node, exp.SetOperation):
                _collect_subq_branches(node.this)
                _collect_subq_branches(node.expression)
            elif isinstance(node, exp.Select):
                subq_branches.append(node)

        _collect_subq_branches(subquery.this)
        if not subq_branches or any(
            len(b.expressions) < len(key_exprs)
            or any(isinstance(e, exp.Star) for e in b.expressions)
            for b in subq_branches
        ):
            self._delete_by_literal_keys(table_name, key_exprs, orig_subquery, extra_conditions)
            return
        first_select = subq_branches[0]
        used_t2_aliases: t.Set[str] = set()
        for idx, col in enumerate(key_exprs):
            col_name = col.name
            t1_col = exp.column(col_name, table="_t1")
            t2_col_name = col_name
            if (
                isinstance(first_select, exp.Select)
                and idx < len(first_select.expressions)
                and not isinstance(first_select.expressions[idx], exp.Star)
            ):
                sel_item = first_select.expressions[idx]
                candidate_name = (
                    sel_item.alias_or_name
                    if isinstance(sel_item, (exp.Column, exp.Alias, exp.Identifier))
                    else ""
                )
                if candidate_name and candidate_name.lower() not in used_t2_aliases:
                    t2_col_name = candidate_name
                else:
                    alias_name = f"_col_{idx}"
                    new_exprs = list(first_select.expressions)
                    inner_expr = sel_item.this if isinstance(sel_item, exp.Alias) else sel_item
                    new_exprs[idx] = exp.alias_(inner_expr.copy(), alias_name, quoted=True)
                    first_select.set("expressions", new_exprs)
                    t2_col_name = alias_name
            used_t2_aliases.add(t2_col_name.lower())
            t2_col = exp.column(t2_col_name, table="_t2")
            join_conditions.append(t1_col.eq(t2_col))

        extra_condition_sets = self._expand_or_free_extra_condition_sets(extra_conditions)
        target_sql = f"{self._to_sql(target_table)} AS `_t1`"
        subquery_with_alias = exp.alias_(subquery.copy(), "_t2", table=True)
        subquery_sql = self._to_sql(subquery_with_alias)

        try:
            for cond_set in extra_condition_sets:
                branch_joins = [jc.copy() for jc in join_conditions]
                for extra in cond_set:
                    qualified_extra = extra.copy()
                    for col in qualified_extra.find_all(exp.Column):
                        if not col.find_ancestor(exp.Subquery, exp.Select) and (
                            not col.table or col.table.lower() == target_table.name.lower()
                        ):
                            col.set("table", exp.to_identifier("_t1"))
                            col.set("db", None)
                            col.set("catalog", None)
                    branch_joins.append(qualified_extra)
                where_expr = exp.and_(*branch_joins)
                where_sql = self._to_sql(where_expr)
                delete_sql = f"DELETE FROM {target_sql} USING {subquery_sql} WHERE {where_sql}"
                self.execute(delete_sql)
        except Exception as e:
            err_msg = str(e).lower()
            if any(
                marker in err_msg
                for marker in (
                    "unique key",
                    "olaptable",
                    "olap table",
                    "not support",
                    "only support",
                    "unsupported",
                )
            ):
                logger.info(
                    "DELETE ... USING not supported on table '%s' (%s); falling back to batched literal DELETE.",
                    table_name,
                    e,
                )
                self._delete_by_literal_keys(table_name, key_exprs, orig_subquery, extra_conditions)
            else:
                raise

    @staticmethod
    def _sanitize_key_value(val: t.Any) -> t.Any:
        if val is None:
            return None
        if isinstance(val, (bytes, bytearray)):
            return _decoded_str(val)
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        if hasattr(val, "item") and not isinstance(
            val,
            (
                str,
                bytes,
                bytearray,
                datetime.date,
                datetime.datetime,
                datetime.time,
                Decimal,
            ),
        ):
            try:
                return val.item()
            except Exception:
                pass
        return val

    @staticmethod
    def _to_delete_literal_expr(val: t.Any, *, is_ast_values: bool = False) -> exp.Expr:
        if is_ast_values and isinstance(val, exp.Expr):
            return val.copy()
        if isinstance(val, (datetime.date, datetime.datetime, datetime.time)):
            return exp.Literal.string(str(val))
        if isinstance(val, Decimal):
            return exp.Literal.number(str(val))
        return exp.convert(val)

    def _delete_by_time_transform_keys(
        self,
        table_name: TableName,
        key_exprs: t.List[exp.Expr],
        subquery: t.Union[exp.Subquery, exp.Select, exp.SetOperation],
        extra_conditions: t.Optional[t.List[exp.Expr]] = None,
    ) -> None:
        """Delete partition ranges for time-truncation partition keys (`DATE_TRUNC`, `DAY`, `MONTH`, `YEAR`, `HOUR`, `TO_DATE`)
        using Doris-compatible half-open bare-column range predicates (`col >= <low> AND col < <high>`).
        """
        for k in key_exprs:
            k_transform = self._extract_time_partition_transform(k)
            if (
                k_transform is not None
                and self._is_date_part_extract_fn(k)
                and k_transform[1] != "year"
            ):
                raise SQLMeshError(
                    f"Doris delete_from cannot rewrite non-year date-part predicate '{k.sql(dialect=self.dialect)}' "
                    "into a contiguous range. Use DATE_TRUNC, TO_DATE, DATE, or YEAR instead."
                )

        select_query = (
            subquery.this.copy() if isinstance(subquery, exp.Subquery) else subquery.copy()
        )
        first_select = select_query
        while isinstance(first_select, (exp.SetOperation, exp.Subquery)):
            first_select = first_select.this
        if not isinstance(first_select, exp.Select):
            return

        projections: t.List[exp.Expr] = []
        raw_year_indices: t.Set[int] = set()
        if (
            isinstance(select_query, exp.Select)
            and len(select_query.expressions) == len(key_exprs)
            and not select_query.args.get("group")
            and not select_query.args.get("having")
            and not select_query.args.get("limit")
            and not select_query.args.get("offset")
        ):
            for idx, k in enumerate(key_exprs):
                sel_item = select_query.expressions[idx]
                raw_sel = sel_item.this if isinstance(sel_item, exp.Alias) else sel_item
                transform = self._extract_time_partition_transform(k)
                if transform is not None:
                    _, unit = transform
                    is_extract_fn = self._is_date_part_extract_fn(k)
                    is_year_fn = unit == "year" and is_extract_fn
                    src_transform = self._extract_time_partition_transform(raw_sel)
                    if (
                        src_transform is not None
                        and src_transform[1] == unit
                        and (not self._is_date_part_extract_fn(raw_sel) or unit == "year")
                    ):
                        src_arg: exp.Expr = src_transform[0].copy()
                    elif is_year_fn:
                        raw_year_indices.add(idx)
                        projections.append(exp.alias_(raw_sel.copy(), f"_val_{idx}", quoted=True))
                        continue
                    elif src_transform is not None or self._is_date_part_extract_fn(raw_sel):
                        raise SQLMeshError(
                            f"Doris delete_from cannot rewrite time-partition predicate '{k.sql(dialect=self.dialect)}' "
                            f"with mismatched subquery projection '{raw_sel.sql(dialect=self.dialect)}' into a contiguous range."
                        )
                    else:
                        src_arg = raw_sel.copy()
                    trunc_expr = exp.Anonymous(
                        this="DATE_TRUNC",
                        expressions=[src_arg, exp.Literal.string(unit)],
                    )
                    upper_expr = exp.DateAdd(
                        this=trunc_expr.copy(),
                        expression=exp.Literal.number(1),
                        unit=exp.var(unit.upper()),
                    )
                    projections.append(exp.alias_(trunc_expr, f"_low_{idx}", quoted=True))
                    projections.append(exp.alias_(upper_expr, f"_high_{idx}", quoted=True))
                else:
                    projections.append(exp.alias_(raw_sel.copy(), f"_val_{idx}", quoted=True))
            select_query.set("order", None)
            select_query.set("expressions", projections)
            fetch_query: exp.Select = select_query.distinct()
        elif len(first_select.expressions) == len(key_exprs):
            all_branches: t.List[exp.Select] = []

            def _collect_select_branches(node: exp.Expr) -> None:
                if isinstance(node, exp.Subquery):
                    _collect_select_branches(node.this)
                elif isinstance(node, exp.SetOperation):
                    _collect_select_branches(node.this)
                    _collect_select_branches(node.expression)
                elif isinstance(node, exp.Select):
                    all_branches.append(node)

            _collect_select_branches(select_query)
            transform_indices = {
                idx
                for idx, k in enumerate(key_exprs)
                if self._extract_time_partition_transform(k) is not None
            }
            unwrapped_indices: t.Set[int] = set(transform_indices)

            # INTERSECT / EXCEPT compare full projection values across branches; unwrapping
            # `DATE_TRUNC(ds, 'day')` into raw `ds` inside branches would change set semantics.
            if any(
                isinstance(op, (exp.Intersect, exp.Except))
                for op in select_query.find_all(exp.SetOperation)
            ):
                unwrapped_indices.clear()

            # Pass 1: Validate which transform indices can be unwrapped across ALL branches.
            for branch in all_branches:
                if (
                    len(branch.expressions) != len(key_exprs)
                    or branch.args.get("limit")
                    or branch.args.get("offset")
                ):
                    unwrapped_indices.clear()
                    break
                has_group = bool(branch.args.get("group"))
                can_unwrap_group = (
                    not branch.args.get("having")
                    and not branch.args.get("limit")
                    and not any(e.find(exp.AggFunc) for e in branch.expressions)
                )
                if has_group and not can_unwrap_group:
                    unwrapped_indices.clear()
                    break
                for idx in list(unwrapped_indices):
                    expected_transform = self._extract_time_partition_transform(key_exprs[idx])
                    expected_unit = expected_transform[1] if expected_transform else ""
                    sel_item = branch.expressions[idx]
                    raw_sel = sel_item.this if isinstance(sel_item, exp.Alias) else sel_item
                    src_transform = self._extract_time_partition_transform(raw_sel)
                    if (
                        src_transform is None
                        or src_transform[1] != expected_unit
                        or (self._is_date_part_extract_fn(raw_sel) and expected_unit != "year")
                    ):
                        unwrapped_indices.discard(idx)

            if any(branch.args.get("group") for branch in all_branches) and (
                unwrapped_indices != transform_indices
            ):
                unwrapped_indices.clear()

            # Pass 2: Mutate branch projections only for indices verified across all branches.
            if unwrapped_indices:
                for branch in all_branches:
                    branch_exprs = list(branch.expressions)
                    for idx in unwrapped_indices:
                        sel_item = branch_exprs[idx]
                        raw_sel = sel_item.this if isinstance(sel_item, exp.Alias) else sel_item
                        src_transform = self._extract_time_partition_transform(raw_sel)
                        assert src_transform is not None
                        branch_exprs[idx] = src_transform[0].copy()
                    if branch.args.get("group"):
                        branch.set("group", None)
                    branch.set("expressions", branch_exprs)

            for idx, k in enumerate(key_exprs):
                transform = self._extract_time_partition_transform(k)
                if transform is not None and idx not in unwrapped_indices:
                    _, unit = transform
                    is_year_fn = unit == "year" and self._is_date_part_extract_fn(k)
                    if not is_year_fn:
                        for branch in all_branches:
                            if idx < len(branch.expressions):
                                b_sel = branch.expressions[idx]
                                b_raw = b_sel.this if isinstance(b_sel, exp.Alias) else b_sel
                                b_trans = self._extract_time_partition_transform(b_raw)
                                if self._is_date_part_extract_fn(b_raw) or (
                                    b_trans is not None and b_trans[1] != unit
                                ):
                                    raise SQLMeshError(
                                        f"Doris delete_from cannot rewrite time-partition predicate '{k.sql(dialect=self.dialect)}' "
                                        f"with mismatched subquery projection '{b_raw.sql(dialect=self.dialect)}' into a contiguous range."
                                    )

            new_first_exprs = list(first_select.expressions)
            for idx, sel_item in enumerate(new_first_exprs):
                inner_expr = sel_item.this if isinstance(sel_item, exp.Alias) else sel_item
                new_first_exprs[idx] = exp.alias_(inner_expr.copy(), f"_col_{idx}", quoted=True)
            first_select.set("expressions", new_first_exprs)

            if not select_query.args.get("limit") and not select_query.args.get("offset"):
                select_query.set("order", None)
            for branch in all_branches:
                if not branch.args.get("limit") and not branch.args.get("offset"):
                    branch.set("order", None)

            for idx, k in enumerate(key_exprs):
                sq_col = exp.column(f"_col_{idx}", quoted=True)
                transform = self._extract_time_partition_transform(k)
                if transform is not None:
                    _, unit = transform
                    is_extract_fn = self._is_date_part_extract_fn(k)
                    is_year_fn = unit == "year" and is_extract_fn
                    if idx not in unwrapped_indices and is_year_fn:
                        raw_year_indices.add(idx)
                        projections.append(exp.alias_(sq_col, f"_val_{idx}", quoted=True))
                        continue
                    trunc_expr = exp.Anonymous(
                        this="DATE_TRUNC",
                        expressions=[sq_col, exp.Literal.string(unit)],
                    )
                    upper_expr = exp.DateAdd(
                        this=trunc_expr.copy(),
                        expression=exp.Literal.number(1),
                        unit=exp.var(unit.upper()),
                    )
                    projections.append(exp.alias_(trunc_expr, f"_low_{idx}", quoted=True))
                    projections.append(exp.alias_(upper_expr, f"_high_{idx}", quoted=True))
                else:
                    projections.append(exp.alias_(sq_col, f"_val_{idx}", quoted=True))
            fetch_query = (
                exp.select(*projections)
                .from_(exp.alias_(exp.Subquery(this=select_query), "_sq", table=True))
                .distinct()
            )
        else:
            return

        rows = self.fetchall(fetch_query, quote_identifiers=True)
        if not rows or not isinstance(rows, (list, tuple)):
            return

        target_table = exp.to_table(table_name)
        cleaned_extra_conditions: t.List[exp.Expr] = []
        for extra in extra_conditions or []:
            cleaned_extra = extra.copy()
            for col in cleaned_extra.find_all(exp.Column):
                if not col.find_ancestor(exp.Subquery, exp.Select) and (
                    col.table and col.table.lower() == target_table.name.lower()
                ):
                    col.set("table", None)
                    col.set("db", None)
                    col.set("catalog", None)
            cleaned_extra_conditions.append(cleaned_extra)
        extra_condition_sets = self._expand_or_free_extra_condition_sets(cleaned_extra_conditions)
        executed_delete_sqls: t.Set[str] = set()

        for row in rows:
            predicates: t.List[exp.Expr] = []
            pos = 0
            for idx, k in enumerate(key_exprs):
                transform = self._extract_time_partition_transform(k)
                if transform is not None:
                    col_expr, _ = transform
                    target_col = exp.column(col_expr.name, quoted=True)
                    if idx in raw_year_indices:
                        yr_val = self._sanitize_key_value(row[pos])
                        pos += 1
                        if yr_val is None:
                            predicates.append(target_col.is_(exp.null()))
                        else:
                            if isinstance(yr_val, (datetime.date, datetime.datetime)):
                                yr_int = yr_val.year
                            elif isinstance(yr_val, str):
                                yr_str = yr_val.strip()
                                yr_int = (
                                    int(yr_str[:4])
                                    if len(yr_str) >= 4 and yr_str[:4].isdigit()
                                    else int(yr_str)
                                )
                            else:
                                yr_int = int(yr_val)
                            low_val: t.Any = f"{yr_int:04d}-01-01"
                            high_val: t.Any = f"{yr_int + 1:04d}-01-01"
                            predicates.append(
                                exp.GTE(
                                    this=target_col.copy(),
                                    expression=self._to_delete_literal_expr(low_val),
                                )
                            )
                            predicates.append(
                                exp.LT(
                                    this=target_col.copy(),
                                    expression=self._to_delete_literal_expr(high_val),
                                )
                            )
                        continue
                    low_val = self._sanitize_key_value(row[pos])
                    high_val = self._sanitize_key_value(row[pos + 1])
                    pos += 2
                    if low_val is None or high_val is None:
                        predicates.append(target_col.is_(exp.null()))
                    else:
                        predicates.append(
                            exp.GTE(
                                this=target_col.copy(),
                                expression=self._to_delete_literal_expr(low_val),
                            )
                        )
                        predicates.append(
                            exp.LT(
                                this=target_col.copy(),
                                expression=self._to_delete_literal_expr(high_val),
                            )
                        )
                else:
                    target_col = exp.column(k.name, quoted=True)
                    val = self._sanitize_key_value(row[pos])
                    pos += 1
                    if val is None:
                        predicates.append(target_col.is_(exp.null()))
                    else:
                        predicates.append(target_col.eq(self._to_delete_literal_expr(val)))
            for extra_set in extra_condition_sets:
                delete_expr = exp.delete(table_name, where=exp.and_(*predicates, *extra_set))
                delete_sql = self._to_sql(delete_expr)
                if delete_sql not in executed_delete_sqls:
                    executed_delete_sqls.add(delete_sql)
                    self.execute(delete_expr)

    def _delete_by_literal_keys(
        self,
        table_name: TableName,
        key_exprs: t.List[exp.Expr],
        subquery: t.Union[exp.Subquery, exp.Select],
        extra_conditions: t.Optional[t.List[exp.Expr]] = None,
    ) -> None:
        """Fallback for DELETE on DUPLICATE KEY / AGGREGATE KEY / Iceberg tables that prohibits OR predicates."""
        select_query = (
            subquery.this.copy() if isinstance(subquery, exp.Subquery) else subquery.copy()
        )
        if isinstance(select_query, exp.Select):
            if select_query.args.get("limit") or select_query.args.get("offset"):
                if not select_query.args.get("order"):
                    select_query = select_query.distinct()
            else:
                select_query.set("order", None)
                select_query = select_query.distinct()
        rows = self.fetchall(select_query, quote_identifiers=True)
        if not rows:
            return
        num_keys = len(key_exprs)
        sanitized_rows = [
            tuple(self._sanitize_key_value(v) for v in row[:num_keys])
            for row in rows
            if len(row) >= num_keys
        ]
        self._delete_key_rows(
            table_name,
            key_exprs,
            sanitized_rows,
            extra_conditions=extra_conditions,
            is_ast_values=False,
        )

    def _delete_key_rows(
        self,
        table_name: TableName,
        key_exprs: t.List[exp.Expr],
        rows: t.Sequence[t.Tuple[t.Any, ...]],
        extra_conditions: t.Optional[t.List[exp.Expr]] = None,
        *,
        is_ast_values: bool = False,
    ) -> None:
        if not rows:
            return

        target_table = exp.to_table(table_name)
        cleaned_extra_conditions: t.List[exp.Expr] = []
        for extra in extra_conditions or []:
            cleaned_extra = extra.copy()
            for col in cleaned_extra.find_all(exp.Column):
                if not col.find_ancestor(exp.Subquery, exp.Select) and (
                    col.table and col.table.lower() == target_table.name.lower()
                ):
                    col.set("table", None)
                    col.set("db", None)
                    col.set("catalog", None)
            cleaned_extra_conditions.append(cleaned_extra)
        extra_condition_sets = self._expand_or_free_extra_condition_sets(cleaned_extra_conditions)
        cols = [exp.column(k.name, quoted=True) for k in key_exprs]
        batch_size = self.DEFAULT_BATCH_SIZE

        def _is_null_val(val: t.Any) -> bool:
            if val is None:
                return True
            if is_ast_values and isinstance(val, exp.Null):
                return True
            return False

        def _to_expr(val: t.Any) -> exp.Expr:
            return self._to_delete_literal_expr(val, is_ast_values=is_ast_values)

        def _dedup_exprs(exprs: t.List[exp.Expr]) -> t.List[exp.Expr]:
            seen: t.Set[str] = set()
            unique_exprs: t.List[exp.Expr] = []
            for e in exprs:
                key_sql = e.sql(dialect=self.dialect)
                if key_sql not in seen:
                    seen.add(key_sql)
                    unique_exprs.append(e)
            return unique_exprs

        if len(cols) == 1:
            col = cols[0]
            non_null_vals = _dedup_exprs([_to_expr(r[0]) for r in rows if not _is_null_val(r[0])])
            has_null = any(_is_null_val(r[0]) for r in rows)

            for i in range(0, len(non_null_vals), batch_size):
                batch = non_null_vals[i : i + batch_size]
                for extra_set in extra_condition_sets:
                    predicates: t.List[exp.Expr] = [col.isin(*batch), *extra_set]
                    self.execute(exp.delete(table_name, where=exp.and_(*predicates)))

            if has_null:
                for extra_set in extra_condition_sets:
                    predicates = [col.is_(exp.null()), *extra_set]
                    self.execute(exp.delete(table_name, where=exp.and_(*predicates)))
            return

        # Composite key (N >= 2): dynamically select the column index to batch via IN (...)
        # that minimizes the number of distinct prefix groups, breaking ties in favor of the last column.
        batch_idx = min(
            range(len(cols)),
            key=lambda idx: (len({row[:idx] + row[idx + 1 :] for row in rows}), -idx),
        )
        grouped: t.Dict[t.Tuple[t.Any, ...], t.List[t.Any]] = defaultdict(list)
        for row in rows:
            prefix_key = row[:batch_idx] + row[batch_idx + 1 :]
            grouped[prefix_key].append(row[batch_idx])

        batch_col = cols[batch_idx]
        prefix_cols = cols[:batch_idx] + cols[batch_idx + 1 :]

        for prefix_vals, batch_vals in grouped.items():
            prefix_preds: t.List[exp.Expr] = [
                p_col.eq(_to_expr(p_val)) if not _is_null_val(p_val) else p_col.is_(exp.null())
                for p_col, p_val in zip(prefix_cols, prefix_vals)
            ]
            non_null_batch = _dedup_exprs([_to_expr(v) for v in batch_vals if not _is_null_val(v)])
            has_null_batch = any(_is_null_val(v) for v in batch_vals)

            for i in range(0, len(non_null_batch), batch_size):
                batch = non_null_batch[i : i + batch_size]
                for extra_set in extra_condition_sets:
                    preds = [*prefix_preds, batch_col.isin(*batch), *extra_set]
                    self.execute(exp.delete(table_name, where=exp.and_(*preds)))

            if has_null_batch:
                for extra_set in extra_condition_sets:
                    preds = [*prefix_preds, batch_col.is_(exp.null()), *extra_set]
                    self.execute(exp.delete(table_name, where=exp.and_(*preds)))

    @classmethod
    def _resolve_table_type_and_keys(
        cls, table_props: t.Dict[str, t.Any], pop: bool = False
    ) -> t.Tuple[t.Any, t.Any, t.Any]:
        get_fn = table_props.pop if pop else table_props.get
        unique_key = get_fn("unique_key", None)
        duplicate_key = get_fn("duplicate_key", None)
        aggregate_key = get_fn("aggregate_key", None)
        raw_table_type = get_fn("table_type", None)
        raw_keys = get_fn("keys", None)

        if raw_table_type is not None and raw_keys is None:
            raise SQLMeshError(
                "Doris property 'table_type' requires 'keys' to be specified in physical_properties."
            )
        if raw_keys is not None:
            tt_str = (
                (cls._extract_str_value(raw_table_type) or "DUPLICATE")
                .strip()
                .strip("'\"`")
                .upper()
            )
            if tt_str in (
                "PRIMARY",
                "PRIMARY_KEY",
                "PRIMARY KEY",
                "UNIQUE",
                "UNIQUE_KEY",
                "UNIQUE KEY",
            ):
                if unique_key is not None:
                    raise SQLMeshError(
                        "Cannot specify both 'unique_key' and 'table_type'/'keys' in physical_properties."
                    )
                unique_key = raw_keys
            elif tt_str in ("DUPLICATE", "DUPLICATE_KEY", "DUPLICATE KEY"):
                if duplicate_key is not None:
                    raise SQLMeshError(
                        "Cannot specify both 'duplicate_key' and 'table_type'/'keys' in physical_properties."
                    )
                duplicate_key = raw_keys
            elif tt_str in ("AGGREGATE", "AGGREGATE_KEY", "AGGREGATE KEY"):
                if aggregate_key is not None:
                    raise SQLMeshError(
                        "Cannot specify both 'aggregate_key' and 'table_type'/'keys' in physical_properties."
                    )
                aggregate_key = raw_keys
            else:
                raise SQLMeshError(
                    f"Invalid Doris table_type '{tt_str}'. Expected PRIMARY, UNIQUE, DUPLICATE, or AGGREGATE."
                )
        return unique_key, duplicate_key, aggregate_key

    def adjust_physical_properties_for_incremental(
        self,
        physical_properties: t.Dict[str, t.Any],
        *,
        requires_delete_capable_table: bool,
        unique_key: t.Optional[t.List[exp.Expr]],
        model_name: str,
    ) -> t.Dict[str, t.Any]:
        """Promote ``unique_key`` from ``INCREMENTAL_BY_UNIQUE_KEY`` models into ``physical_properties``
        when no explicit OLAP key model (``unique_key``, ``duplicate_key``, ``aggregate_key``) is set.
        """
        unique_key_prop, duplicate_key_prop, aggregate_key_prop = self._resolve_table_type_and_keys(
            physical_properties
        )
        if (
            unique_key
            and not unique_key_prop
            and not duplicate_key_prop
            and not aggregate_key_prop
            and all(isinstance(k, (exp.Column, exp.Identifier)) for k in unique_key)
        ):
            cleaned_keys = [exp.column(k.name) for k in unique_key]
            physical_properties = dict(physical_properties)
            physical_properties["unique_key"] = (
                cleaned_keys[0] if len(cleaned_keys) == 1 else exp.Tuple(expressions=cleaned_keys)
            )
        return physical_properties

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
        table_properties = dict(self._get_table_properties(kwargs) or {})
        self._ensure_not_lance_table(
            table_format=kwargs.get("table_format"),
            table_properties=table_properties,
        )
        unique_key_prop, duplicate_key_prop, aggregate_key_prop = self._resolve_table_type_and_keys(
            table_properties
        )

        if (
            primary_key
            and not unique_key_prop
            and not duplicate_key_prop
            and not aggregate_key_prop
        ):
            table_properties["unique_key"] = exp.Tuple(
                expressions=[exp.to_column(col) for col in primary_key]
            )

        kwargs["table_properties"] = table_properties

        super()._create_table_from_columns(
            table_name=table_name,
            target_columns_to_types=target_columns_to_types,
            primary_key=None,
            exists=exists,
            table_description=table_description,
            column_descriptions=column_descriptions,
            **kwargs,
        )

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
        target_table = exp.to_table(table_name)
        is_iceberg = self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=target_table.catalog,
            table_properties=self._get_table_properties(kwargs),
        )
        if (
            not is_iceberg
            and target_columns_to_types
            and columns_to_types_all_known(target_columns_to_types)
        ):
            target_columns_to_types = self._reorder_olap_columns(target_columns_to_types, kwargs)
            if source_columns:
                source_col_set = set(source_columns)
                source_columns = [c for c in target_columns_to_types if c in source_col_set] + [
                    c for c in source_columns if c not in target_columns_to_types
                ]
        super().replace_query(
            table_name,
            query_or_df,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            column_descriptions=column_descriptions,
            source_columns=source_columns,
            supports_replace_table_override=supports_replace_table_override,
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
        if when_matched is None and merge_filter is None:
            self._replace_by_key(
                target_table,
                source_table,
                target_columns_to_types,
                unique_key,
                is_unique_key=True,
                source_columns=source_columns,
                **kwargs,
            )
            return

        self._last_described_key_cols = set()
        table_props = self._get_table_properties(kwargs)
        resolved_uk, duplicate_key_prop, aggregate_key_prop = (
            self._resolve_table_type_and_keys(table_props) if table_props else (None, None, None)
        )
        if not target_columns_to_types or not columns_to_types_all_known(target_columns_to_types):
            target_columns_to_types = self.columns(target_table)
        else:
            table = exp.to_table(target_table)
            is_iceberg = self._is_iceberg_table(
                table_format=kwargs.get("table_format"),
                catalog_name=table.catalog,
                table_properties=table_props,
            )
            if not is_iceberg:
                reorder_kwargs = dict(kwargs)
                if (
                    not duplicate_key_prop
                    and not aggregate_key_prop
                    and all(isinstance(k, (exp.Column, exp.Identifier)) for k in unique_key)
                ):
                    reorder_kwargs["unique_key"] = unique_key
                target_columns_to_types = self._reorder_olap_columns(
                    target_columns_to_types,
                    reorder_kwargs,
                )
                if source_columns:
                    source_col_set = set(source_columns)
                    source_columns = [c for c in target_columns_to_types if c in source_col_set] + [
                        c for c in source_columns if c not in target_columns_to_types
                    ]

        normalized_unique_key: t.List[exp.Expr] = [
            exp.column(k) if isinstance(k, exp.Identifier) else k for k in unique_key
        ]
        key_col_names_lower = {
            c.name.lower() for k in normalized_unique_key for c in k.find_all(exp.Column) if c.name
        }
        key_col_names_lower.update(k.lower() for k in self._last_described_key_cols)
        if resolved_uk:
            key_col_names_lower.update(k.lower() for k in self._extract_column_names(resolved_uk))

        if not when_matched:
            non_key_cols = [
                col for col in target_columns_to_types if col.lower() not in key_col_names_lower
            ]
            update_cols = non_key_cols or list(target_columns_to_types)
            when_matched = exp.Whens(
                expressions=[
                    exp.When(
                        matched=True,
                        source=False,
                        then=exp.Update(
                            expressions=[
                                exp.column(col, MERGE_TARGET_ALIAS).eq(
                                    exp.column(col, MERGE_SOURCE_ALIAS)
                                )
                                for col in update_cols
                            ],
                        ),
                    )
                ]
            )
        else:
            when_matched = when_matched.copy()
            for when_expr in when_matched.find_all(exp.When):
                then_expr = when_expr.args.get("then")
                if isinstance(then_expr, exp.Update):
                    filtered_eqs = [
                        eq_expr
                        for eq_expr in then_expr.expressions
                        if not (
                            isinstance(eq_expr, exp.EQ)
                            and isinstance(eq_expr.this, exp.Column)
                            and eq_expr.this.name.lower() in key_col_names_lower
                        )
                    ]
                    if filtered_eqs:
                        then_expr.set("expressions", filtered_eqs)
                    elif then_expr.expressions:
                        non_key_cols = [
                            col
                            for col in target_columns_to_types
                            if col.lower() not in key_col_names_lower
                        ]
                        if non_key_cols:
                            fallback_col = non_key_cols[0]
                            then_expr.set(
                                "expressions",
                                [
                                    exp.column(fallback_col, MERGE_TARGET_ALIAS).eq(
                                        exp.column(fallback_col, MERGE_TARGET_ALIAS)
                                    )
                                ],
                            )

        super().merge(
            target_table,
            source_table,
            target_columns_to_types,
            normalized_unique_key,
            when_matched=when_matched,
            merge_filter=merge_filter,
            source_columns=source_columns,
            **kwargs,
        )

    def _replace_by_key(
        self,
        target_table: TableName,
        source_table: QueryOrDF,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]],
        key: t.Sequence[exp.Expr],
        is_unique_key: bool,
        source_columns: t.Optional[t.List[str]] = None,
        **kwargs: t.Any,
    ) -> None:
        if not target_columns_to_types or not columns_to_types_all_known(target_columns_to_types):
            target_columns_to_types = self.columns(target_table)

        table = exp.to_table(target_table)
        table_props = self._get_table_properties(kwargs)
        is_iceberg = self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=table.catalog,
            table_properties=table_props,
        )
        normalized_key: t.List[exp.Expr] = [
            exp.column(k.name, quoted=bool(k.quoted)) if isinstance(k, exp.Identifier) else k
            for k in key
        ]
        key_cols: t.List[exp.Column] = []
        if is_unique_key and all(isinstance(k, exp.Column) for k in normalized_key):
            key_cols = [exp.column(k.name) for k in normalized_key]

        has_sequence_col = bool(
            not is_iceberg
            and is_unique_key
            and table_props
            and (
                table_props.get("function_column.sequence_col") is not None
                or table_props.get("function_column.sequence_type") is not None
            )
        )

        temp_table_props: t.Optional[t.Dict[str, t.Any]] = None
        if not is_iceberg:
            col_types_by_lower = {
                c.lower(): dtype for c, dtype in (target_columns_to_types or {}).items()
            }
            norm_key_names_lower = {
                k.name.lower() for k in normalized_key if isinstance(k, exp.Column)
            }
            has_invalid_or_coercible_key_col = any(
                k_low in col_types_by_lower
                and (
                    not self._is_valid_olap_key_type(col_types_by_lower[k_low])
                    or self._is_coercible_string_key_type(col_types_by_lower[k_low])
                )
                for k_low in norm_key_names_lower
            )
            has_other_valid_olap_key_col = any(
                c_low not in norm_key_names_lower
                and self._is_valid_olap_key_type(dtype)
                and not self._is_coercible_string_key_type(dtype)
                for c_low, dtype in col_types_by_lower.items()
            )
            avoid_key_coercion = has_invalid_or_coercible_key_col and has_other_valid_olap_key_col
            if key_cols and not avoid_key_coercion:
                temp_table_props = {
                    "unique_key": (
                        key_cols[0] if len(key_cols) == 1 else exp.Tuple(expressions=key_cols)
                    )
                }
                if table_props:
                    for seq_k in ("function_column.sequence_col", "function_column.sequence_type"):
                        if table_props.get(seq_k) is not None:
                            temp_table_props[seq_k] = table_props[seq_k]
            if target_columns_to_types and columns_to_types_all_known(target_columns_to_types):
                if avoid_key_coercion:
                    reorder_kwargs: t.Dict[str, t.Any] = {}
                elif temp_table_props:
                    reorder_kwargs = {"table_properties": temp_table_props}
                elif normalized_key:
                    reorder_kwargs = {"partitioned_by": list(normalized_key)}
                else:
                    reorder_kwargs = {}
                target_columns_to_types = self._reorder_olap_columns(
                    target_columns_to_types, reorder_kwargs
                )
                if source_columns:
                    source_col_set = set(source_columns)
                    source_columns = [c for c in target_columns_to_types if c in source_col_set] + [
                        c for c in source_columns if c not in target_columns_to_types
                    ]

        temp_table = self._get_temp_table(target_table)
        key_exp = (
            exp.func("CONCAT_WS", "'__SQLMESH_DELIM__'", *normalized_key, dialect=self.dialect)
            if len(normalized_key) > 1
            else normalized_key[0]
        )
        column_names = list(target_columns_to_types or [])

        with self.transaction():
            self.ctas(
                temp_table,
                source_table,
                target_columns_to_types=target_columns_to_types,
                exists=False,
                source_columns=source_columns,
                table_properties=temp_table_props,
                table_format=kwargs.get("table_format"),
            )

            try:
                delete_query = exp.select(key_exp).from_(temp_table)
                insert_query = self._select_columns(target_columns_to_types).from_(temp_table)
                if not is_unique_key:
                    delete_query = delete_query.distinct()
                else:
                    insert_query = insert_query.distinct(*normalized_key)

                insert_statement = exp.insert(
                    insert_query,
                    target_table,
                    columns=column_names,
                )
                delete_filter = key_exp.isin(query=delete_query)

                if not self.INSERT_OVERWRITE_STRATEGY.is_replace_where:
                    if not has_sequence_col:
                        prev_iceberg_ctx = getattr(self, "_in_iceberg_replace_by_key", False)
                        self._in_iceberg_replace_by_key = is_iceberg
                        try:
                            self.delete_from(target_table, delete_filter)
                        finally:
                            self._in_iceberg_replace_by_key = prev_iceberg_ctx
                else:
                    insert_statement.set("where", delete_filter)
                    insert_statement.set("this", exp.to_table(target_table))

                self.execute(insert_statement, track_rows_processed=True)
            finally:
                self.drop_table(temp_table)

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
        table = exp.to_table(target_table)
        is_iceberg = self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=table.catalog,
            table_properties=self._get_table_properties(kwargs),
        )
        if (
            not target_columns_to_types
            or valid_from_col.name not in target_columns_to_types
            or valid_to_col.name not in target_columns_to_types
            or not columns_to_types_all_known(target_columns_to_types)
        ):
            target_columns_to_types = self.columns(target_table)
        if (
            not is_iceberg
            and target_columns_to_types
            and columns_to_types_all_known(target_columns_to_types)
        ):
            target_columns_to_types = self._reorder_olap_columns(target_columns_to_types, kwargs)
            if source_columns:
                source_col_set = set(source_columns)
                source_columns = [c for c in target_columns_to_types if c in source_col_set] + [
                    c for c in source_columns if c not in target_columns_to_types
                ]
        super()._scd_type_2(
            target_table=target_table,
            source_table=source_table,
            unique_key=unique_key,
            valid_from_col=valid_from_col,
            valid_to_col=valid_to_col,
            execution_time=execution_time,
            invalidate_hard_deletes=invalidate_hard_deletes,
            updated_at_col=updated_at_col,
            check_columns=check_columns,
            updated_at_as_valid_from=updated_at_as_valid_from,
            execution_time_as_valid_from=execution_time_as_valid_from,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            column_descriptions=column_descriptions,
            truncate=truncate,
            source_columns=source_columns,
            **kwargs,
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
        self._ensure_not_lance_table(
            table_format=kwargs.get("table_format"),
            table_properties=self._get_table_properties(kwargs),
        )
        table = exp.to_table(table_name)
        effective_catalog = table.catalog or self.default_catalog
        is_iceberg = self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=table.catalog,
            table_properties=self._get_table_properties(kwargs),
        )
        is_internal_iceberg = is_iceberg and (
            not effective_catalog or effective_catalog.lower() == "internal"
        )

        target_columns_to_types_known = bool(
            target_columns_to_types and columns_to_types_all_known(target_columns_to_types)
        )
        if not is_iceberg and target_columns_to_types and target_columns_to_types_known:
            target_columns_to_types = self._reorder_olap_columns(target_columns_to_types, kwargs)
        first_query = source_queries[0].query_factory() if source_queries else None

        def _matches_target_table(t_node: exp.Table) -> bool:
            q_node = quote_identifiers(t_node.copy())
            q_target = quote_identifiers(table.copy())
            if (q_node.args.get("catalog"), q_node.args.get("db"), q_node.this) == (
                q_target.args.get("catalog"),
                q_target.args.get("db"),
                q_target.this,
            ):
                return True
            default_cat = self.default_catalog or "internal"
            norm_node = t_node.copy()
            if not norm_node.catalog and norm_node.db:
                norm_node.set("catalog", exp.parse_identifier(default_cat))
            norm_target = table.copy()
            if not norm_target.catalog and norm_target.db:
                norm_target.set("catalog", exp.parse_identifier(default_cat))
            q_norm_node = quote_identifiers(norm_node)
            q_norm_target = quote_identifiers(norm_target)
            return (
                q_norm_node.args.get("catalog"),
                q_norm_node.args.get("db"),
                q_norm_node.this,
            ) == (
                q_norm_target.args.get("catalog"),
                q_norm_target.args.get("db"),
                q_norm_target.this,
            )

        self_referencing = bool(
            first_query
            and any(_matches_target_table(t_node) for t_node in first_query.find_all(exp.Table))
        )

        # 1. Doris's CTAS grammar forbids `ENGINE=iceberg`, so internal/2-part Iceberg tables with known
        #    column types must be created via schema DDL (`_create_table_from_columns`) + `INSERT INTO`.
        # 2. When `replace_query` is called with a self-referencing query on a non-existent table,
        #    `EngineAdapter.replace_query` creates the empty table via `_create_table_from_columns`
        #    first and then calls `_create_table_from_source_queries(..., replace=False)`. Executing
        #    `CREATE TABLE IF NOT EXISTS ... AS SELECT` would be a no-op, so we insert the source queries.
        if is_internal_iceberg and not target_columns_to_types_known:
            raise SQLMeshError(
                f"Doris does not support `CREATE TABLE ... AS SELECT` with `ENGINE=iceberg` on table '{table_name}' "
                "without known column types. Either qualify the table with an external Iceberg catalog "
                "(`<iceberg_catalog>.<db>.<table>`) or specify explicit column types."
            )

        if (
            is_internal_iceberg or (self_referencing and not replace)
        ) and target_columns_to_types_known:
            assert target_columns_to_types is not None
            if not self_referencing:
                if replace:
                    self.drop_table(table_name, exists=True)
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
                    query = self._order_projections_and_filter(
                        query, target_columns_to_types, coerce_types=True
                    )
                    self._insert_append_query(
                        table_name,
                        query,
                        target_columns_to_types,
                        track_rows_processed=track_rows_processed,
                    )
            return

        super()._create_table_from_source_queries(
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

    @staticmethod
    def _remap_positional_group_and_order(
        node: exp.Expr, old_to_new_one_based: t.Dict[int, int]
    ) -> None:
        if all(k == v for k, v in old_to_new_one_based.items()):
            return

        def _remap_lit(item: exp.Expr) -> None:
            inner = item.this if isinstance(item, exp.Ordered) else item
            if isinstance(inner, exp.Literal) and inner.is_int:
                old_pos = int(inner.this)
                if old_pos in old_to_new_one_based:
                    inner.set("this", str(old_to_new_one_based[old_pos]))
            elif isinstance(inner, (exp.Tuple, exp.Paren)):
                for sub in inner.expressions or ([inner.this] if inner.this else []):
                    _remap_lit(sub)

        group = node.args.get("group")
        if isinstance(group, exp.Group):
            for item in group.expressions:
                _remap_lit(item)
            for prop_key in ("rollup", "cube", "grouping_sets"):
                prop_val = group.args.get(prop_key)
                if isinstance(prop_val, list):
                    for g_item in prop_val:
                        if isinstance(g_item, exp.Expr):
                            _remap_lit(g_item)
                elif isinstance(prop_val, exp.Expr):
                    for g_item in prop_val.expressions:
                        _remap_lit(g_item)
        order = node.args.get("order")
        if isinstance(order, exp.Order):
            for item in order.expressions:
                _remap_lit(item)

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
        if kwargs.get("table_properties") is None and kwargs.get("physical_properties") is not None:
            kwargs["table_properties"] = kwargs["physical_properties"]
        else:
            kwargs.setdefault("table_properties", None)
        self._ensure_not_lance_table(
            table_format=kwargs.get("table_format"),
            table_properties=kwargs.get("table_properties"),
        )
        kwargs["is_schema_ddl"] = isinstance(table_name_or_schema, exp.Schema)

        catalog_name = None
        if isinstance(table_name_or_schema, exp.Schema):
            if isinstance(table_name_or_schema.this, exp.Table):
                catalog_name = table_name_or_schema.this.catalog
        else:
            catalog_name = exp.to_table(table_name_or_schema).catalog

        is_iceberg = self._is_iceberg_table(
            table_format=kwargs.get("table_format"),
            catalog_name=catalog_name,
            table_properties=self._get_table_properties(kwargs),
        )

        if (
            not is_iceberg
            and target_columns_to_types
            and columns_to_types_all_known(target_columns_to_types)
        ):
            original_cols = dict(target_columns_to_types)
            reordered_cols = self._reorder_olap_columns(target_columns_to_types, kwargs)
            order_changed = list(reordered_cols) != list(original_cols)
            types_changed = any(reordered_cols[c] != original_cols[c] for c in reordered_cols)
            query_order_differs = bool(
                isinstance(expression, exp.Select)
                and len(expression.expressions) == len(reordered_cols)
                and [p.alias_or_name.lower() for p in expression.expressions if p.alias_or_name]
                != [c.lower() for c in reordered_cols]
            )
            if order_changed or types_changed or query_order_differs:
                target_columns_to_types = reordered_cols
                if isinstance(table_name_or_schema, exp.Schema):
                    col_def_map = {
                        col_def.name.lower(): col_def
                        for col_def in table_name_or_schema.expressions
                        if isinstance(col_def, exp.ColumnDef)
                    }
                    other_exprs = [
                        e
                        for e in table_name_or_schema.expressions
                        if not isinstance(e, exp.ColumnDef)
                    ]
                    if len(col_def_map) == len(target_columns_to_types) and all(
                        c.lower() in col_def_map for c in target_columns_to_types
                    ):
                        new_col_defs: t.List[exp.Expr] = []
                        for c, new_dtype in target_columns_to_types.items():
                            col_def = col_def_map[c.lower()]
                            if col_def.args.get("kind") != new_dtype:
                                col_def = col_def.copy()
                                col_def.set("kind", new_dtype)
                            new_col_defs.append(col_def)
                        table_name_or_schema = table_name_or_schema.copy()
                        table_name_or_schema.set(
                            "expressions",
                            new_col_defs + other_exprs,
                        )
                if (
                    (order_changed or types_changed or query_order_differs)
                    and expression is not None
                    and isinstance(expression, exp.Query)
                ):
                    reordered_in_place = False
                    if isinstance(expression, exp.Select):
                        proj_by_name = {
                            proj.alias_or_name.lower(): proj
                            for proj in expression.expressions
                            if proj.alias_or_name
                        }
                        if len(proj_by_name) == len(expression.expressions) == len(
                            target_columns_to_types
                        ) and all(c.lower() in proj_by_name for c in target_columns_to_types):
                            old_pos_by_lower = {
                                proj.alias_or_name.lower(): idx + 1
                                for idx, proj in enumerate(expression.expressions)
                                if proj.alias_or_name
                            }
                            expression = expression.copy()
                            updated_projs: t.List[exp.Expr] = []
                            old_to_new_one_based: t.Dict[int, int] = {}
                            for new_idx, (c, new_dtype) in enumerate(
                                target_columns_to_types.items(), start=1
                            ):
                                old_to_new_one_based[old_pos_by_lower[c.lower()]] = new_idx
                                proj = proj_by_name[c.lower()].copy()
                                if types_changed and original_cols[c] != new_dtype:
                                    if isinstance(proj, exp.Alias) and isinstance(
                                        proj.this, exp.Cast
                                    ):
                                        proj.this.set("to", new_dtype.copy())
                                    elif isinstance(proj, exp.Alias):
                                        proj.set(
                                            "this",
                                            exp.cast(proj.this.copy(), to=new_dtype.copy()),
                                        )
                                    elif isinstance(proj, exp.Column):
                                        proj = exp.alias_(
                                            exp.cast(proj.copy(), to=new_dtype.copy()),
                                            proj.name,
                                            quoted=bool(getattr(proj.this, "quoted", True)),
                                        )
                                updated_projs.append(proj)
                            expression.set("expressions", updated_projs)
                            self._remap_positional_group_and_order(expression, old_to_new_one_based)
                            reordered_in_place = True
                    if not reordered_in_place:
                        expression = self._order_projections_and_filter(
                            expression, target_columns_to_types, coerce_types=types_changed
                        )
        elif not is_iceberg and isinstance(expression, (exp.Select, exp.SetOperation)):
            known_col_types = (
                {
                    c.lower(): dtype.copy()
                    for c, dtype in target_columns_to_types.items()
                    if dtype.this != exp.DataType.Type.UNKNOWN
                }
                if target_columns_to_types
                else None
            )
            expression = self._reorder_unannotated_ctas_query(
                expression, kwargs, cte_types=known_col_types
            )
            if target_columns_to_types:
                first_select: t.Optional[exp.Select] = None
                curr_node: t.Optional[exp.Expr] = expression
                while isinstance(curr_node, (exp.SetOperation, exp.Subquery)):
                    curr_node = curr_node.this
                if isinstance(curr_node, exp.Select):
                    first_select = curr_node
                if first_select is not None:
                    col_by_lower = {c.lower(): c for c in target_columns_to_types}
                    reordered_names = [
                        col_by_lower[p.alias_or_name.lower()]
                        for p in first_select.expressions
                        if p.alias_or_name and p.alias_or_name.lower() in col_by_lower
                    ]
                    if len(reordered_names) == len(target_columns_to_types):
                        target_columns_to_types = {
                            c: target_columns_to_types[c] for c in reordered_names
                        }

        # Doris AUTO PARTITION BY RANGE requires the partition column(s) to be defined as NOT NULL.
        if not is_iceberg:
            table_props = self._get_table_properties(kwargs) or {}
            auto_partition = table_props.get("auto_partition")
            is_auto = bool(
                auto_partition is not None
                and str(self._extract_str_value(auto_partition) or "").strip().lower()
                in ("true", "1")
            )
            raw_part_by = (
                kwargs.get("partitioned_by")
                or table_props.get("partitioned_by")
                or table_props.get("partition_by")
            )
            if is_auto and raw_part_by:
                part_list = raw_part_by if isinstance(raw_part_by, list) else [raw_part_by]
                parsed_part_exprs, part_kind = self._parse_partition_expressions(part_list)
                raw_partitions_val = table_props.get("partitions")
                inferred_list_part = (
                    "VALUES IN"
                    in str(
                        self._extract_str_value(raw_partitions_val) or raw_partitions_val or ""
                    ).upper()
                )
                if (part_kind or ("LIST" if inferred_list_part else "RANGE")).upper() != "LIST":
                    auto_range_cols = {
                        col.name.lower()
                        for expr in parsed_part_exprs
                        for col in expr.find_all(exp.Column)
                    }
                    resolved_uk, _, resolved_ak = self._resolve_table_type_and_keys(table_props)
                    strict_key = resolved_uk or kwargs.get("unique_key") or resolved_ak
                    strict_key_set = {k.lower() for k in self._extract_column_names(strict_key)}
                    if strict_key_set and not auto_range_cols.issubset(strict_key_set):
                        auto_range_cols = set()
                    if auto_range_cols and isinstance(table_name_or_schema, exp.Schema):
                        table_name_or_schema = table_name_or_schema.copy()
                        updated_exprs: t.List[exp.Expr] = []
                        for expr in table_name_or_schema.expressions:
                            if (
                                isinstance(expr, exp.ColumnDef)
                                and expr.name.lower() in auto_range_cols
                            ):
                                existing_constraints = list(expr.args.get("constraints") or [])
                                has_not_null = any(
                                    isinstance(c, exp.NotNullColumnConstraint)
                                    or isinstance(
                                        getattr(c, "kind", None), exp.NotNullColumnConstraint
                                    )
                                    for c in existing_constraints
                                )
                                if not has_not_null:
                                    expr = expr.copy()
                                    expr.set(
                                        "constraints",
                                        [exp.ColumnConstraint(kind=exp.NotNullColumnConstraint())]
                                        + existing_constraints,
                                    )
                            updated_exprs.append(expr)
                        table_name_or_schema.set("expressions", updated_exprs)
                    elif auto_range_cols and isinstance(expression, (exp.Select, exp.SetOperation)):
                        expression = expression.copy()
                        auto_select_branches: t.List[exp.Select] = []

                        def _collect_auto_branches(node: exp.Expr) -> None:
                            if isinstance(node, exp.Subquery):
                                _collect_auto_branches(node.this)
                            elif isinstance(node, exp.SetOperation):
                                _collect_auto_branches(node.this)
                                _collect_auto_branches(node.expression)
                            elif isinstance(node, exp.Select):
                                auto_select_branches.append(node)

                        _collect_auto_branches(expression)
                        if auto_select_branches:
                            target_indices = {
                                idx
                                for idx, proj in enumerate(auto_select_branches[0].expressions)
                                if proj.alias_or_name.lower() in auto_range_cols
                            }
                            for branch_idx, branch in enumerate(auto_select_branches):
                                new_select_exprs: t.List[exp.Expr] = []
                                for idx, proj in enumerate(branch.expressions):
                                    if (
                                        proj.alias_or_name.lower() in auto_range_cols
                                        or idx in target_indices
                                    ):
                                        if isinstance(proj, exp.Alias):
                                            inner = proj.this
                                            if not (
                                                isinstance(inner, exp.Anonymous)
                                                and inner.name.upper() == "NON_NULLABLE"
                                            ):
                                                proj = proj.copy()
                                                proj.set(
                                                    "this",
                                                    exp.Anonymous(
                                                        this="NON_NULLABLE",
                                                        expressions=[inner.copy()],
                                                    ),
                                                )
                                        elif isinstance(proj, exp.Column):
                                            wrapped = exp.Anonymous(
                                                this="NON_NULLABLE", expressions=[proj.copy()]
                                            )
                                            proj = (
                                                exp.alias_(
                                                    wrapped,
                                                    proj.name,
                                                    quoted=bool(getattr(proj.this, "quoted", True)),
                                                )
                                                if branch_idx == 0
                                                else wrapped
                                            )
                                        elif not (
                                            isinstance(proj, exp.Anonymous)
                                            and proj.name.upper() == "NON_NULLABLE"
                                        ):
                                            proj = exp.Anonymous(
                                                this="NON_NULLABLE", expressions=[proj.copy()]
                                            )
                                    new_select_exprs.append(proj)
                                branch.set("expressions", new_select_exprs)

        return super()._build_create_table_exp(
            table_name_or_schema,
            expression=expression,
            exists=exists,
            replace=replace,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            table_kind=table_kind,
            **kwargs,
        )

    @staticmethod
    def _is_integer_indicator_projection(proj: exp.Expr) -> bool:
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(inner, exp.Literal) and inner.is_int:
            return True
        if isinstance(inner, exp.If):
            t_val, f_val = inner.args.get("true"), inner.args.get("false")
            return bool(
                isinstance(t_val, exp.Literal)
                and t_val.is_int
                and isinstance(f_val, exp.Literal)
                and f_val.is_int
            )
        if isinstance(inner, exp.Case):
            ifs = inner.args.get("ifs") or []
            default = inner.args.get("default")
            return bool(
                ifs
                and isinstance(default, exp.Literal)
                and default.is_int
                and all(
                    isinstance(i.args.get("true"), exp.Literal) and i.args["true"].is_int
                    for i in ifs
                )
            )
        return False

    def _infer_projection_type(
        self, proj: exp.Expr, cte_types: t.Optional[t.Dict[str, exp.DataType]] = None
    ) -> exp.DataType:
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        inner = inner.unnest()
        while isinstance(inner, exp.Window) and isinstance(inner.this, exp.Expr):
            inner = inner.this.unnest()
        if isinstance(inner, exp.Column) and cte_types and inner.name.lower() in cte_types:
            return cte_types[inner.name.lower()].copy()
        if isinstance(inner, (exp.Cast, exp.TryCast)) and isinstance(inner.to, exp.DataType):
            return inner.to.copy()
        if self._is_integer_indicator_projection(proj):
            return exp.DataType.build("INT", dialect=self.dialect)
        if isinstance(inner, exp.Literal):
            if inner.is_int:
                return exp.DataType.build("INT", dialect=self.dialect)
            if inner.is_number:
                return exp.DataType.build("DOUBLE", dialect=self.dialect)
            if inner.is_string:
                return exp.DataType.build("VARCHAR(255)", dialect=self.dialect)
        if isinstance(inner, exp.Boolean):
            return exp.DataType.build("BOOLEAN", dialect=self.dialect)
        if isinstance(inner, exp.Div):
            return exp.DataType.build("DOUBLE", dialect=self.dialect)
        if isinstance(inner, (exp.Add, exp.Sub, exp.Mul)):
            left_t = self._infer_projection_type(inner.this, cte_types)
            right_t = self._infer_projection_type(inner.expression, cte_types)
            if any(t_cand.is_type("float", "double") for t_cand in (left_t, right_t)):
                return exp.DataType.build("DOUBLE", dialect=self.dialect)
        if isinstance(inner, exp.If):
            branch_types = [
                self._infer_projection_type(b_expr, cte_types)
                for b_expr in (inner.args.get("true"), inner.args.get("false"))
                if isinstance(b_expr, exp.Expr)
            ]
            known_b = [bt for bt in branch_types if bt.this != exp.DataType.Type.UNKNOWN]
            if known_b:
                non_key_b = next(
                    (bt for bt in known_b if not self._is_valid_olap_key_type(bt)), None
                )
                return non_key_b or known_b[0]
        if isinstance(inner, exp.Case):
            case_exprs: t.List[exp.Expr] = [
                i.args["true"]
                for i in (inner.args.get("ifs") or [])
                if isinstance(i.args.get("true"), exp.Expr)
            ]
            if isinstance(inner.args.get("default"), exp.Expr):
                case_exprs.append(inner.args["default"])
            known_c = [
                ct
                for ce in case_exprs
                if (ct := self._infer_projection_type(ce, cte_types)).this
                != exp.DataType.Type.UNKNOWN
            ]
            if known_c:
                non_key_c = next(
                    (ct for ct in known_c if not self._is_valid_olap_key_type(ct)), None
                )
                return non_key_c or known_c[0]
        if isinstance(inner, exp.Coalesce):
            coal_exprs = [inner.this] + list(inner.expressions or [])
            known_coal = [
                ct
                for ce in coal_exprs
                if isinstance(ce, exp.Expr)
                and (ct := self._infer_projection_type(ce, cte_types)).this
                != exp.DataType.Type.UNKNOWN
            ]
            if known_coal:
                non_key_coal = next(
                    (ct for ct in known_coal if not self._is_valid_olap_key_type(ct)), None
                )
                return non_key_coal or known_coal[0]
        if isinstance(
            inner,
            (
                exp.Avg,
                exp.Stddev,
                exp.StddevPop,
                exp.StddevSamp,
                exp.Variance,
                exp.VariancePop,
                exp.EuclideanDistance,
                exp.CosineDistance,
                exp.ManhattanDistance,
                exp.DotProduct,
            ),
        ):
            return exp.DataType.build("DOUBLE", dialect=self.dialect)
        if isinstance(inner, (exp.Array, exp.ArrayAgg)):
            return exp.DataType.build("ARRAY<INT>", dialect=self.dialect)
        if isinstance(inner, (exp.Map, exp.VarMap)):
            return exp.DataType.build("MAP<VARCHAR(255), VARCHAR(255)>", dialect=self.dialect)
        if isinstance(inner, exp.Struct):
            return exp.DataType.build("STRUCT<a: INT>", dialect=self.dialect, udt=True)
        if isinstance(inner, (exp.JSONObject, exp.JSONArray, exp.ParseJSON, exp.JSONExtract)):
            return exp.DataType.build("JSON", dialect=self.dialect)
        if isinstance(inner, exp.Bracket) and isinstance(inner.this, exp.Expr):
            base_t = self._infer_projection_type(inner.this, cte_types)
            base_type_name = (
                base_t.this.value.upper()
                if isinstance(base_t.this, exp.DataType.Type)
                else str(base_t.this).upper()
            )
            if base_type_name in ("VARIANT", "JSON", "JSONB"):
                return base_t
        if isinstance(inner, exp.GroupConcat):
            return exp.DataType.build("TEXT", dialect=self.dialect)
        if isinstance(inner, (exp.Count, exp.RowNumber, exp.Rank, exp.DenseRank, exp.Ntile)):
            return exp.DataType.build("BIGINT", dialect=self.dialect)
        if isinstance(
            inner,
            (
                exp.Year,
                exp.Month,
                exp.Day,
                exp.Hour,
                exp.Minute,
                exp.Second,
                exp.DayOfWeek,
                exp.DayOfYear,
                exp.WeekOfYear,
            ),
        ):
            return exp.DataType.build("INT", dialect=self.dialect)
        if isinstance(inner, (exp.CurrentDate, exp.Date, exp.TsOrDsToDate)):
            return exp.DataType.build("DATE", dialect=self.dialect)
        if isinstance(inner, (exp.CurrentTimestamp, exp.DateTrunc, exp.TimestampTrunc)):
            return exp.DataType.build("DATETIME", dialect=self.dialect)
        if isinstance(inner, exp.Anonymous):
            fn_upper = inner.name.upper()
            if fn_upper in (
                "AVG",
                "STDDEV",
                "STDDEV_POP",
                "STDDEV_SAMP",
                "VARIANCE",
                "VAR_POP",
                "VAR_SAMP",
                "L2_DISTANCE",
                "L2_DISTANCE_APPROXIMATE",
                "COSINE_DISTANCE",
                "COSINE_DISTANCE_APPROXIMATE",
                "INNER_PRODUCT",
                "INNER_PRODUCT_APPROXIMATE",
                "L1_DISTANCE",
            ):
                return exp.DataType.build("DOUBLE", dialect=self.dialect)
            if fn_upper in ("ARRAY", "ARRAY_AGG", "COLLECT_LIST", "COLLECT_SET"):
                return exp.DataType.build("ARRAY<INT>", dialect=self.dialect)
            if fn_upper in ("MAP", "MAP_AGG", "VARIANT_TYPE", "JSON_OBJECT_FLATTEN"):
                return exp.DataType.build("MAP<VARCHAR(255), VARCHAR(255)>", dialect=self.dialect)
            if fn_upper in ("STRUCT", "NAMED_STRUCT"):
                return exp.DataType.build("STRUCT<a: INT>", dialect=self.dialect, udt=True)
            if fn_upper in (
                "JSON_OBJECT",
                "JSON_ARRAY",
                "TO_JSON",
                "JSON_PARSE",
                "JSONB_PARSE",
                "JSON_EXTRACT",
            ):
                return exp.DataType.build("JSON", dialect=self.dialect)
            if fn_upper in (
                "BITMAP_UNION",
                "BITMAP_EMPTY",
                "TO_BITMAP",
                "BITMAP_HASH",
                "BITMAP_HASH64",
                "BITMAP_FROM_STRING",
            ):
                return exp.DataType.build("BITMAP", dialect=self.dialect, udt=True)
            if fn_upper in ("HLL_UNION", "HLL_EMPTY", "HLL_HASH", "HLL_FROM_BASE64"):
                return exp.DataType.build("HLL", dialect=self.dialect, udt=True)
            if fn_upper in ("TO_QUANTILE_STATE", "QUANTILE_UNION"):
                return exp.DataType.build("QUANTILE_STATE", dialect=self.dialect, udt=True)
            if fn_upper == "GROUP_CONCAT":
                return exp.DataType.build("TEXT", dialect=self.dialect)
            if fn_upper in ("COUNT", "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE"):
                return exp.DataType.build("BIGINT", dialect=self.dialect)
            if fn_upper == "DATE_TRUNC":
                return exp.DataType.build("DATETIME", dialect=self.dialect)
            if fn_upper == "TO_DATE":
                return exp.DataType.build("DATE", dialect=self.dialect)
            if fn_upper == "IS_VALID_UTF8":
                return exp.DataType.build("BOOLEAN", dialect=self.dialect)
        return exp.DataType.build("UNKNOWN")

    @staticmethod
    def _projection_key_name(proj: exp.Expr, idx: int) -> str:
        if isinstance(proj, exp.Star):
            return ""
        if proj.alias_or_name:
            return proj.alias_or_name
        if (
            isinstance(proj, (exp.Cast, exp.TryCast))
            and isinstance(proj.this, exp.Column)
            and proj.this.name
        ):
            return proj.this.name
        return f"__unaliased_col_{idx}"

    def _reorder_unannotated_ctas_query(
        self,
        expression: exp.Expr,
        kwargs: t.Dict[str, t.Any],
        cte_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> exp.Expr:
        cte_col_types: t.Dict[str, exp.DataType] = dict(cte_types or {})
        with_node = expression.args.get("with_")
        if isinstance(with_node, exp.With):
            for cte_item in with_node.expressions:
                if isinstance(cte_item, exp.CTE) and isinstance(cte_item.this, exp.Select):
                    for cte_proj in cte_item.this.expressions:
                        if cte_proj.alias_or_name and not isinstance(cte_proj, exp.Star):
                            inferred_cte_t = self._infer_projection_type(cte_proj, cte_col_types)
                            if inferred_cte_t.this != exp.DataType.Type.UNKNOWN:
                                cte_col_types[cte_proj.alias_or_name.lower()] = inferred_cte_t
        if isinstance(expression, exp.SetOperation):
            expression = expression.copy()
            branches: t.List[exp.Select] = []

            def _collect_branches(node: exp.Expr) -> None:
                if isinstance(node, exp.Subquery):
                    _collect_branches(node.this)
                elif isinstance(node, exp.SetOperation):
                    _collect_branches(node.this)
                    _collect_branches(node.expression)
                elif isinstance(node, exp.Select):
                    branches.append(node)

            _collect_branches(expression)
            if not branches:
                return expression
            first_exprs = branches[0].expressions
            num_cols = len(first_exprs)
            orig_names = [self._projection_key_name(p, i) for i, p in enumerate(first_exprs)]
            if (
                num_cols > 0
                and all(
                    len(b.expressions) == num_cols
                    and not any(isinstance(p, exp.Star) for p in b.expressions)
                    for b in branches
                )
                and all(orig_names)
                and len({n.lower() for n in orig_names}) == num_cols
            ):
                merged_cols_to_types: t.Dict[str, exp.DataType] = {}
                for col_idx, col_name in enumerate(orig_names):
                    branch_cands = [
                        cand
                        for b in branches
                        if (
                            cand := self._infer_projection_type(
                                b.expressions[col_idx], cte_col_types
                            )
                        ).this
                        != exp.DataType.Type.UNKNOWN
                    ]
                    non_coercible_invalid = next(
                        (
                            c
                            for c in branch_cands
                            if not self._is_valid_olap_key_type(c)
                            and not self._is_coercible_string_key_type(c)
                        ),
                        None,
                    )
                    coercible_str = next(
                        (c for c in branch_cands if self._is_coercible_string_key_type(c)),
                        None,
                    )
                    merged_cols_to_types[col_name] = (
                        non_coercible_invalid
                        or coercible_str
                        or (branch_cands[0] if branch_cands else exp.DataType.build("UNKNOWN"))
                    )
                reordered_select_cols = self._reorder_olap_columns(merged_cols_to_types, kwargs)
                perm = [orig_names.index(c) for c in reordered_select_cols]
                coercions = {
                    orig_names.index(c): reordered_select_cols[c]
                    for c in reordered_select_cols
                    if reordered_select_cols[c] != merged_cols_to_types[c]
                }
                if perm != list(range(num_cols)) or coercions:
                    old_to_new_one_based = {
                        orig_idx + 1: new_idx + 1 for new_idx, orig_idx in enumerate(perm)
                    }
                    for b_idx, branch in enumerate(branches):
                        old_projs = list(branch.expressions)
                        updated_branch_projs: t.List[exp.Expr] = []
                        for orig_idx in perm:
                            proj = old_projs[orig_idx].copy()
                            new_dtype = coercions.get(orig_idx)
                            if new_dtype is not None:
                                col_name = orig_names[orig_idx]
                                is_synthetic = col_name.startswith("__unaliased_col_")
                                if isinstance(proj, exp.Alias) and isinstance(
                                    proj.this, (exp.Cast, exp.TryCast)
                                ):
                                    proj.this.set("to", new_dtype.copy())
                                elif isinstance(proj, (exp.Cast, exp.TryCast)):
                                    cast_expr = exp.cast(proj.this.copy(), to=new_dtype.copy())
                                    proj = (
                                        exp.alias_(cast_expr, col_name, quoted=True)
                                        if b_idx == 0 and not is_synthetic
                                        else cast_expr
                                    )
                                elif isinstance(proj, exp.Alias):
                                    proj.set(
                                        "this", exp.cast(proj.this.copy(), to=new_dtype.copy())
                                    )
                                elif isinstance(proj, exp.Column):
                                    cast_expr = exp.cast(proj.copy(), to=new_dtype.copy())
                                    proj = (
                                        exp.alias_(
                                            cast_expr,
                                            proj.name,
                                            quoted=bool(getattr(proj.this, "quoted", True)),
                                        )
                                        if b_idx == 0
                                        else cast_expr
                                    )
                                else:
                                    cast_expr = exp.cast(proj.copy(), to=new_dtype.copy())
                                    proj = (
                                        exp.alias_(cast_expr, col_name, quoted=True)
                                        if b_idx == 0 and not is_synthetic
                                        else cast_expr
                                    )
                            updated_branch_projs.append(proj)
                        branch.set("expressions", updated_branch_projs)
                        self._remap_positional_group_and_order(branch, old_to_new_one_based)
                    self._remap_positional_group_and_order(expression, old_to_new_one_based)
                return expression
            expression.set(
                "this",
                self._reorder_unannotated_ctas_query(expression.this, kwargs, cte_col_types),
            )
            expression.set(
                "expression",
                self._reorder_unannotated_ctas_query(expression.expression, kwargs, cte_col_types),
            )
            return expression
        if isinstance(expression, exp.Select):
            keyed_projs = [
                (self._projection_key_name(proj, idx), proj)
                for idx, proj in enumerate(expression.expressions)
                if not isinstance(proj, exp.Star)
            ]
            proj_by_name = {k.lower(): proj for k, proj in keyed_projs if k}
            if proj_by_name and len(proj_by_name) == len(expression.expressions):
                select_cols_to_types = {
                    k: self._infer_projection_type(proj, cte_col_types) for k, proj in keyed_projs
                }
                reordered_select_cols = self._reorder_olap_columns(select_cols_to_types, kwargs)
                order_changed = list(reordered_select_cols) != list(select_cols_to_types)
                types_changed = any(
                    reordered_select_cols[c] != select_cols_to_types[c]
                    for c in reordered_select_cols
                )
                if order_changed or types_changed:
                    old_pos_by_lower = {
                        k.lower(): idx + 1 for idx, (k, _) in enumerate(keyed_projs)
                    }
                    expression = expression.copy()
                    updated_projs: t.List[exp.Expr] = []
                    old_to_new_one_based = {}
                    for new_idx, (c, new_dtype) in enumerate(
                        reordered_select_cols.items(), start=1
                    ):
                        old_to_new_one_based[old_pos_by_lower[c.lower()]] = new_idx
                        proj = proj_by_name[c.lower()].copy()
                        if types_changed and select_cols_to_types[c] != new_dtype:
                            is_synthetic = c.startswith("__unaliased_col_")
                            if isinstance(proj, exp.Alias) and isinstance(
                                proj.this, (exp.Cast, exp.TryCast)
                            ):
                                proj.this.set("to", new_dtype.copy())
                            elif isinstance(proj, (exp.Cast, exp.TryCast)):
                                cast_expr = exp.cast(proj.this.copy(), to=new_dtype.copy())
                                proj = (
                                    cast_expr
                                    if is_synthetic
                                    else exp.alias_(cast_expr, c, quoted=True)
                                )
                            elif isinstance(proj, exp.Alias):
                                proj.set("this", exp.cast(proj.this.copy(), to=new_dtype.copy()))
                            elif isinstance(proj, exp.Column):
                                proj = exp.alias_(
                                    exp.cast(proj.copy(), to=new_dtype.copy()),
                                    proj.name,
                                    quoted=bool(getattr(proj.this, "quoted", True)),
                                )
                            else:
                                cast_expr = exp.cast(proj.copy(), to=new_dtype.copy())
                                proj = (
                                    cast_expr
                                    if is_synthetic
                                    else exp.alias_(cast_expr, c, quoted=True)
                                )
                        updated_projs.append(proj)
                    expression.set("expressions", updated_projs)
                    self._remap_positional_group_and_order(expression, old_to_new_one_based)
            elif any(isinstance(proj, exp.Star) for proj in expression.expressions):
                table_props = self._get_table_properties(kwargs) or {}
                resolved_uk, resolved_dk, resolved_ak = self._resolve_table_type_and_keys(
                    table_props
                )
                has_explicit_key = bool(
                    resolved_uk or kwargs.get("unique_key") or resolved_dk or resolved_ak
                )
                with_clause = expression.args.get("with_")
                from_clause = expression.args.get("from_")
                if (
                    isinstance(with_clause, exp.With)
                    and isinstance(from_clause, exp.From)
                    and isinstance(from_clause.this, exp.Table)
                ):
                    target_cte_name = from_clause.this.name.lower()
                    for cte_idx, cte in enumerate(with_clause.expressions):
                        if (
                            isinstance(cte, exp.CTE)
                            and cte.alias.lower() == target_cte_name
                            and isinstance(cte.this, exp.Select)
                            and len(cte.this.expressions) >= 2
                        ):
                            indicator_projs = [
                                p
                                for p in cte.this.expressions
                                if self._is_integer_indicator_projection(p)
                            ]
                            other_projs = [
                                p
                                for p in cte.this.expressions
                                if not self._is_integer_indicator_projection(p)
                            ]
                            if (
                                not has_explicit_key
                                and indicator_projs
                                and list(cte.this.expressions[: len(indicator_projs)])
                                != indicator_projs
                            ):
                                expression = expression.copy()
                                copied_cte = expression.args["with_"].expressions[cte_idx]
                                copied_select = copied_cte.this
                                copied_select.set("expressions", indicator_projs + other_projs)
                            elif (
                                len(expression.expressions) == 1
                                and isinstance(expression.expressions[0], exp.Star)
                                and not expression.args.get("joins")
                            ):
                                reordered_cte_sel = self._reorder_unannotated_ctas_query(
                                    cte.this, kwargs, cte_col_types
                                )
                                if reordered_cte_sel != cte.this:
                                    expression = expression.copy()
                                    copied_cte = expression.args["with_"].expressions[cte_idx]
                                    copied_cte.set("this", reordered_cte_sel)
                            break
        return expression

    @classmethod
    def _is_coercible_string_key_type(cls, dtype: exp.DataType) -> bool:
        type_name = (
            dtype.this.value.upper()
            if isinstance(dtype.this, exp.DataType.Type)
            else str(dtype.this).upper()
        )
        return type_name in ("TEXT", "STRING", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT")

    def _reorder_olap_columns(
        self,
        target_columns_to_types: t.Dict[str, exp.DataType],
        kwargs: t.Optional[t.Dict[str, t.Any]] = None,
    ) -> t.Dict[str, exp.DataType]:
        kwargs = kwargs or {}
        table_props = self._get_table_properties(kwargs) or {}
        if table_props.get("primary_key") is not None or kwargs.get("primary_key") is not None:
            raise SQLMeshError(
                "Doris does not support 'primary_key' in physical_properties; use 'unique_key', 'duplicate_key', or 'aggregate_key' instead."
            )
        if table_props.get("partition") is not None:
            raise SQLMeshError(
                "Invalid property 'partition' in physical_properties; did you mean 'partition_by', 'partitioned_by', or 'partitions'?"
            )
        if (
            sum(
                x is not None
                for x in (
                    kwargs.get("partitioned_by") or None,
                    table_props.get("partitioned_by"),
                    table_props.get("partition_by"),
                )
            )
            > 1
        ):
            raise SQLMeshError(
                "Multiple partition definitions found: specify only one of model 'partitioned_by' or physical_properties 'partitioned_by' / 'partition_by'."
            )
        resolved_uk, duplicate_key, aggregate_key = self._resolve_table_type_and_keys(table_props)
        unique_key = resolved_uk or kwargs.get("unique_key")

        specified_key_types = [
            name
            for name, val in (
                ("unique_key", unique_key),
                ("duplicate_key", duplicate_key),
                ("aggregate_key", aggregate_key),
            )
            if self._extract_column_names(val)
        ]
        if len(specified_key_types) > 1:
            raise SQLMeshError(
                f"Doris tables support at most one key model (unique_key, duplicate_key, or aggregate_key), "
                f"got multiple: {', '.join(specified_key_types)}."
            )

        explicit_key = unique_key or duplicate_key or aggregate_key
        order_by = table_props.get("order_by") or table_props.get("cluster_by")
        order_by_cols = self._extract_column_names(order_by)
        if order_by_cols and not unique_key:
            raise SQLMeshError(
                "Doris table property 'order_by' is only supported on UNIQUE KEY tables. "
                "For DUPLICATE KEY or AGGREGATE KEY tables, sort order is determined by the key columns."
            )
        mow_raw = self._extract_str_value(table_props.get("enable_unique_key_merge_on_write"))
        if order_by_cols and mow_raw is not None and mow_raw.strip().lower() in ("false", "0"):
            raise SQLMeshError(
                "Doris table property 'order_by' on UNIQUE KEY tables requires Merge-on-Write "
                "('enable_unique_key_merge_on_write' = 'true')."
            )
        seq_col_raw = self._extract_str_value(table_props.get("function_column.sequence_col"))
        seq_type_raw = self._extract_str_value(table_props.get("function_column.sequence_type"))
        if (seq_col_raw or seq_type_raw) and not unique_key:
            raise SQLMeshError(
                "Doris 'function_column.sequence_col' / 'function_column.sequence_type' is only supported on UNIQUE KEY tables."
            )

        col_names = list(target_columns_to_types)
        if not col_names:
            return target_columns_to_types

        if explicit_key:
            col_by_lower = {c.lower(): c for c in col_names}
            raw_key_cols = self._extract_column_names(explicit_key)
            missing_cols = [k for k in raw_key_cols if k.lower() not in col_by_lower]
            if missing_cols:
                raise SQLMeshError(
                    f"Doris key columns {missing_cols} not found in table schema columns {col_names}."
                )
            if seq_col_raw and seq_col_raw.strip("`").lower() not in col_by_lower:
                raise SQLMeshError(
                    f"Doris function_column.sequence_col '{seq_col_raw}' not found in table schema columns {col_names}."
                )
            key_cols = [col_by_lower[k.lower()] for k in raw_key_cols if k.lower() in col_by_lower]
            if duplicate_key and not unique_key and not aggregate_key:
                raw_part_by_dup = (
                    kwargs.get("partitioned_by")
                    or table_props.get("partitioned_by")
                    or table_props.get("partition_by")
                )
                has_active_olap_part = bool(
                    raw_part_by_dup
                    and (
                        table_props.get("partitions")
                        or kwargs.get("partitions")
                        or str(self._extract_str_value(table_props.get("auto_partition")) or "")
                        .strip()
                        .lower()
                        in ("true", "1")
                        or str(
                            self._extract_str_value(table_props.get("dynamic_partition.enable"))
                            or ""
                        )
                        .strip()
                        .lower()
                        in ("true", "1")
                    )
                )
                if has_active_olap_part:
                    part_list_dup = (
                        raw_part_by_dup if isinstance(raw_part_by_dup, list) else [raw_part_by_dup]
                    )
                    parsed_part_dup, _ = self._parse_partition_expressions(part_list_dup)
                    key_cols_lower = {k.lower() for k in key_cols}
                    for expr in parsed_part_dup:
                        for col_node in expr.find_all(exp.Column):
                            canonical = col_by_lower.get(col_node.name.lower())
                            if canonical and canonical.lower() not in key_cols_lower:
                                key_cols.append(canonical)
                                key_cols_lower.add(canonical.lower())
            updated_types = dict(target_columns_to_types)
            for k in key_cols:
                k_dtype = updated_types[k]
                if not self._is_valid_olap_key_type(k_dtype):
                    if self._is_coercible_string_key_type(k_dtype):
                        updated_types[k] = exp.DataType.build("VARCHAR(255)", dialect=self.dialect)
                    else:
                        raise SQLMeshError(
                            f"Doris key column '{k}' has type '{k_dtype.sql(dialect=self.dialect)}' which is not supported as an OLAP key column. "
                            "Use an integer, DATE/DATETIME, DECIMAL, CHAR, or VARCHAR(n) column instead."
                        )
            if order_by_cols:
                missing_order_cols = [k for k in order_by_cols if k.lower() not in col_by_lower]
                if missing_order_cols:
                    raise SQLMeshError(
                        f"Doris order_by columns {missing_order_cols} not found in table schema columns {col_names}."
                    )
                for ob_raw in order_by_cols:
                    ob_col = col_by_lower[ob_raw.lower()]
                    ob_dtype = updated_types[ob_col]
                    if not self._is_valid_olap_key_type(ob_dtype):
                        if self._is_coercible_string_key_type(ob_dtype):
                            updated_types[ob_col] = exp.DataType.build(
                                "VARCHAR(255)", dialect=self.dialect
                            )
                        else:
                            raise SQLMeshError(
                                f"Doris order_by column '{ob_col}' has type '{ob_dtype.sql(dialect=self.dialect)}' which is not supported as an OLAP sort key column. "
                                "Use an integer, DATE/DATETIME, DECIMAL, CHAR, or VARCHAR(n) column instead."
                            )
            key_cols_set = set(key_cols)
            new_order = (
                key_cols + [c for c in col_names if c not in key_cols_set]
                if key_cols and col_names[: len(key_cols)] != key_cols
                else col_names
            )
            return {c: updated_types[c] for c in new_order}

        updated_types = dict(target_columns_to_types)
        raw_part_by = (
            kwargs.get("partitioned_by")
            or table_props.get("partitioned_by")
            or table_props.get("partition_by")
        )
        part_col_names: t.List[str] = []
        if raw_part_by:
            part_list = raw_part_by if isinstance(raw_part_by, list) else [raw_part_by]
            parsed_part_exprs, _ = self._parse_partition_expressions(part_list)
            col_by_lower = {c.lower(): c for c in col_names}
            for expr in parsed_part_exprs:
                for col in expr.find_all(exp.Column):
                    canonical = col_by_lower.get(col.name.lower())
                    if canonical and canonical not in part_col_names:
                        part_col_names.append(canonical)
                        if not self._is_valid_olap_key_type(
                            updated_types[canonical]
                        ) and self._is_coercible_string_key_type(updated_types[canonical]):
                            updated_types[canonical] = exp.DataType.build(
                                "VARCHAR(255)", dialect=self.dialect
                            )

        first_dtype = updated_types[col_names[0]]
        if not self._is_valid_olap_key_type(first_dtype):
            known_valid_col = next(
                (
                    c
                    for c in col_names
                    if self._is_valid_olap_key_type(updated_types[c])
                    and not updated_types[c].is_type("unknown", "null")
                ),
                None,
            )
            coercible_col = next(
                (c for c in col_names if self._is_coercible_string_key_type(updated_types[c])),
                None,
            )
            unknown_valid_col = next(
                (c for c in col_names if self._is_valid_olap_key_type(updated_types[c])),
                None,
            )
            if known_valid_col:
                col_names = [known_valid_col] + [c for c in col_names if c != known_valid_col]
            elif coercible_col:
                updated_types[coercible_col] = exp.DataType.build(
                    "VARCHAR(255)", dialect=self.dialect
                )
                if col_names[0] != coercible_col:
                    col_names = [coercible_col] + [c for c in col_names if c != coercible_col]
            elif unknown_valid_col:
                col_names = [unknown_valid_col] + [c for c in col_names if c != unknown_valid_col]

        # Ensure OLAP partition/time columns are included in Doris's implicit DUPLICATE KEY prefix:
        # Doris stops implicit key inference at the first non-keyable column, immediately after the
        # first VARCHAR column, or after 3 columns (`shortkey_max_column_count`) when no physical
        # OLAP PARTITION BY clause is emitted.
        if part_col_names:
            part_cols = [
                c for c in part_col_names if self._is_valid_olap_key_type(updated_types[c])
            ]
            part_cols_set = set(part_cols)
            first_non_key_idx = next(
                (
                    i
                    for i, c in enumerate(col_names)
                    if not self._is_valid_olap_key_type(updated_types[c])
                ),
                None,
            )
            first_varchar_idx = next(
                (
                    i
                    for i, c in enumerate(col_names)
                    if c not in part_cols_set and updated_types[c].is_type("varchar")
                ),
                None,
            )
            auto_str = self._extract_str_value(table_props.get("auto_partition"))
            dyn_str = self._extract_str_value(table_props.get("dynamic_partition.enable"))
            has_physical_olap_partition = bool(
                table_props.get("partitions")
                or (auto_str and str(auto_str).strip().lower() in ("true", "1"))
                or (dyn_str and str(dyn_str).strip().lower() in ("true", "1"))
            )
            shortkey_limit_idx = (
                max(1, 3 - len(part_cols)) if not has_physical_olap_partition else None
            )
            cutoff_candidates = [
                idx
                for idx in (first_non_key_idx, first_varchar_idx, shortkey_limit_idx)
                if idx is not None
            ]
            cutoff_idx = min(cutoff_candidates) if cutoff_candidates else None
            if (
                part_cols
                and cutoff_idx is not None
                and any(col_names.index(p) >= cutoff_idx for p in part_cols)
            ):
                moved_parts = [p for p in part_cols if col_names.index(p) >= cutoff_idx]
                moved_set = set(moved_parts)
                before_cutoff = [c for c in col_names[:cutoff_idx] if c not in moved_set]
                after_cutoff = [c for c in col_names[cutoff_idx:] if c not in moved_set]
                col_names = before_cutoff + moved_parts + after_cutoff

        if col_names != list(target_columns_to_types) or updated_types != target_columns_to_types:
            return {c: updated_types[c] for c in col_names}

        return target_columns_to_types

    def _parse_partition_expressions(
        self, partitioned_by: t.List[exp.Expr]
    ) -> t.Tuple[t.List[exp.Expr], t.Optional[str]]:
        """Parse partition expressions and extract partition kind and normalized expressions."""
        parsed_partitioned_by: t.List[exp.Expr] = []
        partition_kind: t.Optional[str] = None

        flattened_partitioned_by: t.List[exp.Expr] = []
        for item in partitioned_by:
            if isinstance(item, (exp.Tuple, exp.Array)):
                flattened_partitioned_by.extend(item.expressions)
            else:
                flattened_partitioned_by.append(item)

        for expr in flattened_partitioned_by:
            if isinstance(expr, exp.List) or (
                isinstance(expr, exp.Anonymous)
                and expr.this
                and str(expr.this).upper() in ("RANGE", "LIST")
            ):
                partition_kind = "LIST" if isinstance(expr, exp.List) else str(expr.this).upper()
                for arg in expr.expressions:
                    if isinstance(arg, (exp.Column, exp.Func, exp.Anonymous)):
                        parsed_partitioned_by.append(arg)
                    else:
                        parsed_partitioned_by.append(exp.to_column(str(arg)))
                continue

            if isinstance(expr, exp.Literal) and getattr(expr, "is_string", False):
                text = str(expr.this)
                match = re.match(r"^\s*(RANGE|LIST)\s*\((.+)\)\s*$", text, flags=re.IGNORECASE)
                if match:
                    partition_kind = match.group(1).upper()
                    inner = match.group(2).strip()
                    if ";" in inner:
                        raise SQLMeshError(f"Invalid partition expression: {text}")
                    try:
                        parsed_stmts = parse(f"SELECT {inner}", read=self.dialect)
                        if (
                            len(parsed_stmts) == 1
                            and isinstance(parsed_stmts[0], exp.Select)
                            and set(parsed_stmts[0].args.keys()) <= {"expressions"}
                        ):
                            for sel_expr in parsed_stmts[0].expressions:
                                parsed_partitioned_by.append(sel_expr)
                            continue
                    except Exception:
                        pass
                    inner_cols = [c.strip().strip("`") for c in inner.split(",") if c.strip()]
                    for col in inner_cols:
                        parsed_partitioned_by.append(exp.to_column(col))
                    continue

                if ";" in text or "--" in text or "/*" in text:
                    raise SQLMeshError(f"Invalid partition expression: {text}")
                try:
                    parsed_stmts = parse(f"SELECT {text}", read=self.dialect)
                    if (
                        len(parsed_stmts) == 1
                        and isinstance(parsed_stmts[0], exp.Select)
                        and set(parsed_stmts[0].args.keys()) <= {"expressions"}
                    ):
                        for sel_expr in parsed_stmts[0].expressions:
                            parsed_partitioned_by.append(sel_expr)
                        continue
                except Exception:
                    pass
                parsed_partitioned_by.append(exp.to_column(text.strip().strip("`")))
                continue

            parsed_partitioned_by.append(expr)

        return parsed_partitioned_by, partition_kind

    @staticmethod
    def _to_raw_partition_sql(expr: t.Union[exp.Literal, exp.Var, str, t.Any]) -> exp.Var:
        if isinstance(expr, exp.Literal):
            raw_text = str(expr.this)
        elif isinstance(expr, exp.Var):
            raw_text = str(expr.this)
        elif isinstance(expr, str):
            raw_text = expr
        else:
            return expr
        if ";" in raw_text or "--" in raw_text or "/*" in raw_text:
            raise SQLMeshError(f"Invalid partition definition expression: {raw_text}")
        return exp.Var(this=raw_text, quoted=False)

    def _build_partitioned_by_exp(
        self,
        partitioned_by: t.List[exp.Expr],
        *,
        partition_interval_unit: t.Optional[IntervalUnit] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        catalog_name: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[
        t.Union[
            exp.PartitionedByProperty,
            exp.PartitionByRangeProperty,
            exp.PartitionByListProperty,
            exp.Property,
        ]
    ]:
        """Build Doris partitioning expression for OLAP (manual, dynamic, AUTO PARTITION) and Iceberg tables."""
        is_iceberg = bool(kwargs.get("is_iceberg"))
        partitions = kwargs.get("partitions")
        auto_partition = kwargs.get("auto_partition")
        dynamic_partition_enabled = kwargs.get("dynamic_partition_enabled")

        partitioned_by, partition_kind = self._parse_partition_expressions(partitioned_by)
        if target_columns_to_types:
            col_by_lower = {c.lower(): c for c in target_columns_to_types}
            normalized_exprs: t.List[exp.Expr] = []
            for expr in partitioned_by:
                expr_copy = expr.copy()
                for col_node in expr_copy.find_all(exp.Column):
                    canonical = col_by_lower.get(col_node.name.lower())
                    if canonical and canonical != col_node.name:
                        col_node.set("this", exp.to_identifier(canonical))
                normalized_exprs.append(expr_copy)
            partitioned_by = normalized_exprs

        if is_iceberg:

            def _normalize_iceberg_part_node(node: exp.Expr) -> exp.Expr:
                if isinstance(node, exp.PartitionedByBucket):
                    return exp.Anonymous(
                        this="BUCKET",
                        expressions=[node.expression.copy(), node.this.copy()],
                    )
                if isinstance(node, exp.PartitionByTruncate):
                    return exp.Anonymous(
                        this="TRUNCATE",
                        expressions=[node.expression.copy(), node.this.copy()],
                    )
                if isinstance(node, exp.Trunc):
                    dec = node.args.get("decimals")
                    if isinstance(node.this, exp.Column) and isinstance(dec, exp.Literal):
                        return exp.Anonymous(
                            this="TRUNCATE",
                            expressions=[dec.copy(), node.this.copy()],
                        )
                    if isinstance(node.this, exp.Literal) and isinstance(dec, exp.Column):
                        return exp.Anonymous(
                            this="TRUNCATE",
                            expressions=[node.this.copy(), dec.copy()],
                        )
                if (
                    isinstance(node, exp.Anonymous)
                    and node.name.upper() in ("BUCKET", "TRUNCATE")
                    and len(node.expressions) == 2
                    and isinstance(node.expressions[0], exp.Column)
                    and isinstance(node.expressions[1], exp.Literal)
                ):
                    return exp.Anonymous(
                        this=node.name.upper(),
                        expressions=[node.expressions[1].copy(), node.expressions[0].copy()],
                    )
                return node

            iceberg_part_exprs = [
                expr.copy().transform(_normalize_iceberg_part_node) for expr in partitioned_by
            ]
            return exp.PartitionByListProperty(
                partition_expressions=iceberg_part_exprs,
                create_expressions=[],
            )

        is_auto = False
        if auto_partition is not None:
            auto_str = self._extract_str_value(auto_partition)
            is_auto = str(auto_str).strip().lower() in ("true", "1")

        # On Doris OLAP tables, if no explicit partitions, auto_partition, or dynamic_partition.enable
        # are configured, omit PARTITION BY so Doris does not create a 0-partition table that rejects inserts.
        if not partitions and not is_auto and not dynamic_partition_enabled:
            logger.warning(
                "[Doris] Omitting PARTITION BY on OLAP table because none of 'auto_partition = true', "
                "'dynamic_partition.enable = true', or explicit 'partitions' were configured in physical_properties. "
                "Add one of those properties to physical_properties, or set 'partition_by_time_column false' in your "
                "INCREMENTAL_BY_TIME_RANGE model kind to silence this warning."
            )
            return None

        create_expressions: t.Optional[t.List[exp.Expr]] = None
        if partitions:
            if isinstance(partitions, exp.Tuple):
                create_expressions = [self._to_raw_partition_sql(e) for e in partitions.expressions]
            else:
                create_expressions = [self._to_raw_partition_sql(partitions)]
        elif is_auto or dynamic_partition_enabled:
            create_expressions = []

        inferred_list = False
        if partition_kind is None and create_expressions:
            try:
                texts = [getattr(e, "this", "").upper() for e in create_expressions]
                inferred_list = any("VALUES IN" in text for text in texts)
            except Exception:
                inferred_list = False
        if partition_kind:
            is_list = str(partition_kind).upper() == "LIST"
        else:
            is_list = inferred_list

        partition_prop: t.Union[exp.PartitionByListProperty, exp.PartitionByRangeProperty]
        if is_list:
            partition_prop = exp.PartitionByListProperty(
                partition_expressions=partitioned_by,
                create_expressions=create_expressions,
            )
        else:
            partition_prop = exp.PartitionByRangeProperty(
                partition_expressions=partitioned_by,
                create_expressions=create_expressions,
            )

        if is_auto:
            return exp.SetConfigProperty(this=exp.var(f"AUTO {self._to_sql(partition_prop)}"))

        return partition_prop

    def _build_table_properties_exp(
        self,
        catalog_name: t.Optional[str] = None,
        table_format: t.Optional[str] = None,
        storage_format: t.Optional[str] = None,
        partitioned_by: t.Optional[t.List[exp.Expr]] = None,
        partition_interval_unit: t.Optional[IntervalUnit] = None,
        clustered_by: t.Optional[t.List[exp.Expr]] = None,
        table_properties: t.Optional[t.Dict[str, t.Any]] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        table_kind: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        """Creates a SQLGlot table properties expression for Doris DDL."""
        properties: t.List[exp.Expr] = []
        is_materialized_view = table_kind == "MATERIALIZED_VIEW"
        is_schema_ddl = bool(kwargs.get("is_schema_ddl"))

        if is_materialized_view:
            properties.append(exp.MaterializedProperty())

        table_properties_copy = dict(table_properties) if table_properties else {}
        is_iceberg = not is_materialized_view and self._is_iceberg_table(
            table_format=table_format,
            catalog_name=catalog_name,
            table_properties=table_properties_copy,
        )

        # Pop Iceberg/Paimon/Hive engine property if present so it is not duplicated in PROPERTIES (...)
        explicit_engine = self._extract_str_value(table_properties_copy.pop("engine", None)) or (
            str(table_format).strip().lower() if table_format else None
        )
        engine_lower = explicit_engine.strip().lower() if explicit_engine else None
        if is_iceberg and is_schema_ddl:
            engine_name = engine_lower if engine_lower in ("hive", "paimon") else "iceberg"
            properties.append(exp.EngineProperty(this=exp.var(engine_name)))

        if storage_format and not is_materialized_view:
            sf_str = str(storage_format).strip().strip("'").strip('"')
            if sf_str:
                if is_iceberg:
                    if engine_lower == "paimon":
                        table_properties_copy.setdefault(
                            "file.format", exp.Literal.string(sf_str.lower())
                        )
                    elif engine_lower == "hive":
                        table_properties_copy.setdefault(
                            "file_format", exp.Literal.string(sf_str.lower())
                        )
                    elif (
                        "write-format" not in table_properties_copy
                        and "write.format.default" not in table_properties_copy
                    ):
                        table_properties_copy["write-format"] = exp.Literal.string(sf_str.lower())
                elif "storage_format" not in table_properties_copy:
                    table_properties_copy["storage_format"] = exp.Literal.string(sf_str.upper())

        if is_materialized_view:
            build_val = table_properties_copy.pop("build", None)
            build_mode_val = table_properties_copy.pop("build_mode", None)
            if build_val is None and build_mode_val is not None:
                build_val = build_mode_val
            if build_val is not None:
                build_upper = self._to_upper_str(build_val)
                if build_upper not in ("IMMEDIATE", "DEFERRED"):
                    raise SQLMeshError(
                        f"Invalid materialized view build mode: {build_upper}. Expected IMMEDIATE or DEFERRED."
                    )
                properties.append(exp.BuildProperty(this=exp.Var(this=build_upper)))

            refresh_val = table_properties_copy.pop("refresh", None)
            for refresh_alias in ("refresh_mode", "refresh_method"):
                alias_val = table_properties_copy.pop(refresh_alias, None)
                if refresh_val is None and alias_val is not None:
                    refresh_val = alias_val
            refresh_trigger_val = table_properties_copy.pop("refresh_trigger", None)
            if refresh_val is not None or refresh_trigger_val is not None:
                method_var: exp.Var = exp.Var(this="AUTO")
                kind_str: t.Optional[str] = None
                every_lit: t.Optional[exp.Expr] = None
                unit_var: t.Optional[exp.Var] = None
                starts_lit: t.Optional[exp.Literal] = None

                if isinstance(refresh_val, exp.Paren) and isinstance(refresh_val.this, exp.EQ):
                    refresh_val = exp.Tuple(expressions=[refresh_val.this])
                elif isinstance(refresh_val, exp.EQ):
                    refresh_val = exp.Tuple(expressions=[refresh_val])

                if isinstance(refresh_val, exp.Tuple):
                    method_var, kind_str, every_lit, unit_var, starts_lit = (
                        self._parse_refresh_tuple(refresh_val)
                    )
                    if refresh_trigger_val is not None:
                        trigger_text = self._extract_str_value(refresh_trigger_val) or ""
                        k, e, u, s = self._parse_trigger_string(trigger_text)
                        kind_str, every_lit, unit_var, starts_lit = k, e, u, s
                else:
                    raw_refresh = (
                        self._extract_str_value(refresh_val) if refresh_val is not None else "AUTO"
                    )
                    raw_refresh_str = (raw_refresh or "AUTO").strip()
                    if " " in raw_refresh_str:
                        parts = raw_refresh_str.split(None, 1)
                        method_upper = parts[0].upper()
                        if method_upper in ("COMPLETE", "AUTO", "FAST", "FORCE", "NEVER"):
                            method_var = exp.Var(this=method_upper)
                            k, e, u, s = self._parse_trigger_string(parts[1])
                            kind_str, every_lit, unit_var, starts_lit = k, e, u, s
                        else:
                            k, e, u, s = self._parse_trigger_string(raw_refresh_str)
                            kind_str, every_lit, unit_var, starts_lit = k, e, u, s
                    else:
                        method_upper = raw_refresh_str.upper()
                        if method_upper in ("MANUAL", "COMMIT"):
                            kind_str = method_upper
                        elif method_upper in ("COMPLETE", "AUTO", "FAST", "FORCE", "NEVER"):
                            method_var = exp.Var(this=method_upper)
                        else:
                            raise SQLMeshError(
                                f"Invalid materialized view refresh method: {method_upper}. Expected one of: COMPLETE, AUTO, FAST, FORCE, NEVER, MANUAL, COMMIT."
                            )

                    if refresh_trigger_val is not None:
                        trigger_text = self._extract_str_value(refresh_trigger_val) or ""
                        k, e, u, s = self._parse_trigger_string(trigger_text)
                        kind_str, every_lit, unit_var, starts_lit = k, e, u, s

                properties.append(
                    exp.RefreshTriggerProperty(
                        method=method_var,
                        kind=kind_str,
                        every=every_lit,
                        unit=unit_var,
                        starts=starts_lit,
                    )
                )

        # Pop key models and order_by from table_properties_copy
        if (
            table_properties_copy.pop("primary_key", None) is not None
            or kwargs.get("primary_key") is not None
        ):
            raise SQLMeshError(
                "Doris does not support 'primary_key' in physical_properties; use 'unique_key', 'duplicate_key', or 'aggregate_key' instead."
            )
        if table_properties_copy.get("partition") is not None:
            raise SQLMeshError(
                "Invalid property 'partition' in physical_properties; did you mean 'partition_by', 'partitioned_by', or 'partitions'?"
            )
        resolved_uk, duplicate_key, aggregate_key = self._resolve_table_type_and_keys(
            table_properties_copy, pop=True
        )
        unique_key = resolved_uk or kwargs.get("unique_key")
        order_by = table_properties_copy.pop("order_by", None) or table_properties_copy.pop(
            "cluster_by", None
        )

        unique_key_cols = self._extract_column_names(unique_key)
        duplicate_key_cols = self._extract_column_names(duplicate_key)
        aggregate_key_cols = self._extract_column_names(aggregate_key)
        order_by_cols = self._extract_column_names(order_by)
        col_by_lower = (
            {c.lower(): c for c in target_columns_to_types} if target_columns_to_types else {}
        )

        # Pop partitioning-related properties before building key properties so explicit duplicate_key
        # can automatically include active OLAP partition columns (required by Doris FE validation).
        partitions = table_properties_copy.pop("partitions", None)
        auto_partition = table_properties_copy.pop("auto_partition", None)
        raw_part_by = table_properties_copy.pop("partitioned_by", None)
        raw_part_by_alias = table_properties_copy.pop("partition_by", None)
        if (
            sum(
                x is not None
                for x in (
                    partitioned_by or None,
                    raw_part_by,
                    raw_part_by_alias,
                )
            )
            > 1
        ):
            raise SQLMeshError(
                "Multiple partition definitions found: specify only one of model 'partitioned_by' or physical_properties 'partitioned_by' / 'partition_by'."
            )
        if not partitioned_by:
            chosen_part_by = raw_part_by if raw_part_by is not None else raw_part_by_alias
            if chosen_part_by is not None:
                partitioned_by = (
                    chosen_part_by if isinstance(chosen_part_by, list) else [chosen_part_by]
                )

        if (
            is_iceberg
            and engine_lower == "paimon"
            and unique_key_cols
            and "primary-key" not in table_properties_copy
        ):
            paimon_pk_cols = [col_by_lower.get(k.lower(), k) for k in unique_key_cols]
            if partitioned_by:
                parsed_paimon_part, _ = self._parse_partition_expressions(partitioned_by)
                pk_lower_set = {k.lower() for k in paimon_pk_cols}
                for expr in parsed_paimon_part:
                    for col_node in expr.find_all(exp.Column):
                        part_col_name = col_by_lower.get(col_node.name.lower(), col_node.name)
                        if part_col_name.lower() not in pk_lower_set:
                            paimon_pk_cols.append(part_col_name)
                            pk_lower_set.add(part_col_name.lower())
            table_properties_copy["primary-key"] = exp.Literal.string(",".join(paimon_pk_cols))

        if not is_iceberg:
            specified_key_types = [
                name
                for name, cols in (
                    ("unique_key", unique_key_cols),
                    ("duplicate_key", duplicate_key_cols),
                    ("aggregate_key", aggregate_key_cols),
                )
                if cols
            ]
            if len(specified_key_types) > 1:
                raise SQLMeshError(
                    f"Doris tables support at most one key model (unique_key, duplicate_key, or aggregate_key), "
                    f"got multiple: {', '.join(specified_key_types)}."
                )
            active_key_cols = unique_key_cols or duplicate_key_cols or aggregate_key_cols
            if active_key_cols and col_by_lower and not is_materialized_view:
                missing_cols = [k for k in active_key_cols if k.lower() not in col_by_lower]
                if missing_cols:
                    raise SQLMeshError(
                        f"Doris key columns {missing_cols} not found in table schema columns {list(target_columns_to_types or ())}."
                    )
                if unique_key_cols:
                    unique_key_cols = [col_by_lower[k.lower()] for k in unique_key_cols]
                elif duplicate_key_cols:
                    duplicate_key_cols = [col_by_lower[k.lower()] for k in duplicate_key_cols]
                elif aggregate_key_cols:
                    aggregate_key_cols = [col_by_lower[k.lower()] for k in aggregate_key_cols]

            if (
                duplicate_key_cols
                and partitioned_by
                and not is_materialized_view
                and (
                    partitions
                    or str(self._extract_str_value(auto_partition) or "").strip().lower()
                    in ("true", "1")
                    or str(
                        self._extract_str_value(
                            table_properties_copy.get("dynamic_partition.enable")
                        )
                        or ""
                    )
                    .strip()
                    .lower()
                    in ("true", "1")
                )
            ):
                parsed_dup_part_exprs, _ = self._parse_partition_expressions(partitioned_by)
                dup_lower_set = {k.lower() for k in duplicate_key_cols}
                for expr in parsed_dup_part_exprs:
                    for col_node in expr.find_all(exp.Column):
                        part_col_name = col_by_lower.get(col_node.name.lower(), col_node.name)
                        if part_col_name.lower() not in dup_lower_set:
                            duplicate_key_cols.append(part_col_name)
                            dup_lower_set.add(part_col_name.lower())

            if unique_key_cols:
                properties.append(
                    exp.UniqueKeyProperty(expressions=[exp.to_column(k) for k in unique_key_cols])
                )
            elif duplicate_key_cols:
                properties.append(
                    exp.DuplicateKeyProperty(
                        expressions=[exp.to_column(k) for k in duplicate_key_cols]
                    )
                )
            elif aggregate_key_cols:
                agg_cols_sql = ", ".join(self._to_sql(exp.to_column(k)) for k in aggregate_key_cols)
                properties.append(
                    exp.SetConfigProperty(this=exp.var(f"AGGREGATE KEY ({agg_cols_sql})"))
                )

            if order_by_cols and not is_materialized_view:
                if not unique_key_cols:
                    raise SQLMeshError(
                        "Doris table property 'order_by' is only supported on UNIQUE KEY tables. "
                        "For DUPLICATE KEY or AGGREGATE KEY tables, sort order is determined by the key columns."
                    )
                mow_val = self._extract_str_value(
                    table_properties_copy.get("enable_unique_key_merge_on_write")
                )
                if mow_val is not None and mow_val.strip().lower() in ("false", "0"):
                    raise SQLMeshError(
                        "Doris table property 'order_by' on UNIQUE KEY tables requires Merge-on-Write "
                        "('enable_unique_key_merge_on_write' = 'true')."
                    )
                if col_by_lower:
                    missing_order_cols = [k for k in order_by_cols if k.lower() not in col_by_lower]
                    if missing_order_cols:
                        raise SQLMeshError(
                            f"Doris order_by columns {missing_order_cols} not found in table schema columns {list(target_columns_to_types or ())}."
                        )
                    order_by_cols = [col_by_lower[k.lower()] for k in order_by_cols]
                order_cols_sql = ", ".join(self._to_sql(exp.to_column(k)) for k in order_by_cols)
                properties.append(
                    exp.SetConfigProperty(this=exp.var(f"ORDER BY ({order_cols_sql})"))
                )

            if not is_materialized_view:
                seq_col_val = self._extract_str_value(
                    table_properties_copy.get("function_column.sequence_col")
                )
                seq_type_val = self._extract_str_value(
                    table_properties_copy.get("function_column.sequence_type")
                )
                if (seq_col_val or seq_type_val) and not unique_key_cols:
                    raise SQLMeshError(
                        "Doris 'function_column.sequence_col' / 'function_column.sequence_type' is only supported on UNIQUE KEY tables."
                    )
                if seq_col_val and col_by_lower:
                    seq_col_clean = seq_col_val.strip("`")
                    if seq_col_clean.lower() not in col_by_lower:
                        raise SQLMeshError(
                            f"Doris function_column.sequence_col '{seq_col_clean}' not found in table schema columns {list(target_columns_to_types or ())}."
                        )
                    table_properties_copy["function_column.sequence_col"] = exp.Literal.string(
                        col_by_lower[seq_col_clean.lower()]
                    )
                compaction_policy_val = self._extract_str_value(
                    table_properties_copy.get("compaction_policy")
                )
                if compaction_policy_val is not None:
                    cp_lower = compaction_policy_val.strip().lower()
                    if cp_lower not in ("size_based", "time_series"):
                        raise SQLMeshError(
                            f"Invalid Doris compaction_policy '{compaction_policy_val}'. Expected 'size_based' or 'time_series'."
                        )
                    table_properties_copy["compaction_policy"] = exp.Literal.string(cp_lower)

        if table_description:
            properties.append(
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        add_partition = True
        if partitioned_by:
            parsed_part_exprs, _ = self._parse_partition_expressions(partitioned_by)
            strict_part_key_cols = unique_key_cols or aggregate_key_cols
            if strict_part_key_cols and not is_materialized_view and not is_iceberg:
                key_cols_set = {k.lower() for k in strict_part_key_cols}
                partition_cols = set()
                for expr in parsed_part_exprs:
                    for col_node in expr.find_all(exp.Column):
                        partition_cols.add(col_node.name.lower())
                not_in_key = partition_cols - key_cols_set
                if not_in_key:
                    key_label = "UNIQUE KEY" if unique_key_cols else "AGGREGATE KEY"
                    logger.warning(
                        f"[Doris] {key_label} model: partitioned_by columns {not_in_key} not in key_cols {key_cols_set}, skip PARTITION BY."
                    )
                    add_partition = False

            if add_partition:
                if is_materialized_view:
                    if parsed_part_exprs:
                        properties.append(
                            exp.PartitionedByProperty(
                                this=exp.Schema(expressions=parsed_part_exprs),
                            )
                        )
                else:
                    dyn_enable = self._extract_str_value(
                        table_properties_copy.get("dynamic_partition.enable")
                    )
                    dynamic_partition_enabled = bool(
                        dyn_enable and dyn_enable.strip().lower() in ("true", "1")
                    )
                    partition_expr = self._build_partitioned_by_exp(
                        partitioned_by,
                        partition_interval_unit=partition_interval_unit,
                        target_columns_to_types=target_columns_to_types,
                        catalog_name=catalog_name,
                        partitions=partitions,
                        auto_partition=auto_partition,
                        dynamic_partition_enabled=dynamic_partition_enabled,
                        is_iceberg=is_iceberg,
                    )
                    if partition_expr:
                        if col_by_lower:
                            part_col_nodes = [
                                col_node
                                for expr in parsed_part_exprs
                                for col_node in expr.find_all(exp.Column)
                            ]
                            missing_part_cols = [
                                c.name for c in part_col_nodes if c.name.lower() not in col_by_lower
                            ]
                            if missing_part_cols:
                                raise SQLMeshError(
                                    f"Doris partitioned_by columns {missing_part_cols} not found in table schema columns {list(target_columns_to_types or ())}."
                                )
                        properties.append(partition_expr)

        # Pop distribution / bucketing properties so they never leak into PROPERTIES (...)
        distributed_by = table_properties_copy.pop("distributed_by", None)
        top_level_buckets = table_properties_copy.pop("buckets", None)
        prop_clustered_by = table_properties_copy.pop("clustered_by", None)
        effective_clustered_by = clustered_by or (
            [exp.to_column(c) for c in self._extract_column_names(prop_clustered_by)]
            if prop_clustered_by
            else None
        )

        if not is_iceberg:
            strict_key_cols = (
                (unique_key_cols or aggregate_key_cols) if not is_materialized_view else []
            )
            dist_prop = self._build_distributed_by_exp(
                distributed_by=distributed_by,
                clustered_by=effective_clustered_by,
                unique_key_cols=strict_key_cols,
                fallback_key_cols=duplicate_key_cols if not is_materialized_view else [],
                top_level_buckets=top_level_buckets,
            )
            if dist_prop is not None:
                if not is_materialized_view:
                    dist_kind = str(dist_prop.args.get("kind") or "").upper()
                    if strict_key_cols and dist_kind == "RANDOM":
                        key_kind_label = "UNIQUE KEY" if unique_key_cols else "AGGREGATE KEY"
                        raise SQLMeshError(
                            f"Doris {key_kind_label} tables do not support DISTRIBUTED BY RANDOM. "
                            "Use DISTRIBUTED BY HASH (...) instead."
                        )
                    if col_by_lower and dist_prop.expressions:
                        dist_col_names = [c.name for c in dist_prop.expressions]
                        missing_dist_cols = [
                            c for c in dist_col_names if c.lower() not in col_by_lower
                        ]
                        if missing_dist_cols:
                            raise SQLMeshError(
                                f"Doris distributed_by columns {missing_dist_cols} not found in table schema columns {list(target_columns_to_types or ())}."
                            )
                        dist_prop.set(
                            "expressions",
                            [exp.to_column(col_by_lower[c.lower()]) for c in dist_col_names],
                        )
                    if strict_key_cols and dist_prop.expressions:
                        strict_key_set = {k.lower() for k in strict_key_cols}
                        non_key_dist_cols = [
                            c.name
                            for c in dist_prop.expressions
                            if c.name.lower() not in strict_key_set
                        ]
                        if non_key_dist_cols:
                            key_kind_label = "UNIQUE KEY" if unique_key_cols else "AGGREGATE KEY"
                            raise SQLMeshError(
                                f"Doris {key_kind_label} tables require distributed_by columns to be key columns, "
                                f"got {non_key_dist_cols} which are not in {strict_key_cols}."
                            )
                properties.append(dist_prop)

        # Default replication_num = 1 for single-BE dev/CI when not explicitly configured on OLAP tables
        # Note: Only emit explicit table_properties_copy entries so unit test expectations remain clean.
        if table_properties_copy:
            properties.extend(self._properties_to_expressions(table_properties_copy))

        if properties:
            return exp.Properties(expressions=properties)
        return None

    def _to_upper_str(self, val: t.Any) -> str:
        extracted = self._extract_str_value(val)
        if extracted is not None:
            return extracted.strip().strip("`").upper()
        return str(val).strip().strip("`").upper()

    def _parse_refresh_tuple(
        self,
        tpl: exp.Tuple,
    ) -> t.Tuple[
        exp.Var,
        t.Optional[str],
        t.Optional[exp.Expr],
        t.Optional[exp.Var],
        t.Optional[exp.Literal],
    ]:
        method_var: exp.Var = exp.Var(this="AUTO")
        kind_str: t.Optional[str] = None
        every_lit: t.Optional[exp.Expr] = None
        unit_var: t.Optional[exp.Var] = None
        starts_lit: t.Optional[exp.Literal] = None
        for e in tpl.expressions:
            if isinstance(e, exp.EQ):
                key = (
                    (self._extract_str_value(e.this) or "").strip('"').strip("'").strip("`").lower()
                )
                val = e.expression
                if key == "method":
                    method_upper = self._to_upper_str(val)
                    if method_upper not in ("COMPLETE", "AUTO", "FAST", "FORCE", "NEVER"):
                        raise SQLMeshError(
                            f"Invalid materialized view refresh method: {method_upper}. Expected one of: COMPLETE, AUTO, FAST, FORCE, NEVER."
                        )
                    method_var = exp.Var(this=method_upper)
                elif key == "kind":
                    kind_upper = self._to_upper_str(val)
                    if kind_upper not in ("MANUAL", "COMMIT", "SCHEDULE"):
                        raise SQLMeshError(
                            f"Invalid materialized view refresh kind: {kind_upper}. Expected one of: MANUAL, COMMIT, SCHEDULE."
                        )
                    kind_str = kind_upper
                elif key == "every":
                    if isinstance(val, exp.Literal):
                        val_str = str(val.this).strip()
                        if not val_str.isdigit():
                            raise SQLMeshError(
                                f"Invalid materialized view refresh every interval: {val_str}. Expected a positive integer."
                            )
                        every_lit = exp.Literal.number(int(val_str))
                    elif isinstance(val, (int, float)):
                        every_lit = exp.Literal.number(int(val))
                elif key == "unit":
                    unit_upper = self._to_upper_str(val)
                    if not re.match(r"^[A-Z]+$", unit_upper):
                        raise SQLMeshError(f"Invalid materialized view refresh unit: {unit_upper}")
                    unit_var = exp.Var(this=unit_upper)
                elif key == "starts":
                    starts_str = self._extract_str_value(val)
                    starts_lit = exp.Literal.string(
                        starts_str if starts_str is not None else str(val)
                    )
        return method_var, kind_str, every_lit, unit_var, starts_lit

    @staticmethod
    def _parse_trigger_string(
        trigger_text: str,
    ) -> t.Tuple[
        t.Optional[str],
        t.Optional[exp.Expr],
        t.Optional[exp.Var],
        t.Optional[exp.Literal],
    ]:
        text = trigger_text.strip()
        if text.upper().startswith("ON "):
            text = text[3:].strip()
        kind = None
        every_lit: t.Optional[exp.Expr] = None
        unit_var = None
        starts_lit = None

        m_kind = re.match(r"^(MANUAL|COMMIT|SCHEDULE)\b", text, flags=re.IGNORECASE)
        if not m_kind:
            raise SQLMeshError(
                f"Invalid materialized view refresh trigger: {trigger_text}. Expected MANUAL, COMMIT, or SCHEDULE."
            )
        kind = m_kind.group(1).upper()
        m_every = re.search(r"\bEVERY\s+(\d+)\s+([A-Za-z]+)", text, flags=re.IGNORECASE)
        if m_every:
            every_lit = exp.Literal.number(int(m_every.group(1)))
            unit_var = exp.Var(this=m_every.group(2).upper())
        m_starts = re.search(r"\bSTARTS\s+'([^']+)'", text, flags=re.IGNORECASE)
        if m_starts:
            starts_lit = exp.Literal.string(m_starts.group(1))
        return kind, every_lit, unit_var, starts_lit

    def _resolve_buckets(self, raw_buckets: t.Any) -> t.Optional[exp.Expr]:
        if raw_buckets is None:
            return None
        if isinstance(raw_buckets, exp.Literal):
            val_str = str(raw_buckets.this).strip()
            if val_str.upper() == "AUTO":
                return exp.Var(this="AUTO")
            if val_str.isdigit():
                return exp.Literal.number(int(val_str))
        if isinstance(raw_buckets, int):
            return exp.Literal.number(raw_buckets)
        val_str = str(self._extract_str_value(raw_buckets) or "").strip()
        if val_str.upper() == "AUTO":
            return exp.Var(this="AUTO")
        if val_str.isdigit():
            return exp.Literal.number(int(val_str))
        raise SQLMeshError(
            f"Invalid buckets value for Doris DISTRIBUTED BY: {raw_buckets}. Expected a positive integer or 'AUTO'."
        )

    @classmethod
    def _eval_int_limit_expr(cls, node: t.Optional[exp.Expr]) -> t.Optional[int]:
        if node is None:
            return None
        curr = node.unnest()
        if isinstance(curr, exp.Literal) and not curr.is_string:
            try:
                return int(str(curr.this).strip())
            except ValueError:
                return None
        if isinstance(curr, (exp.Add, exp.Sub, exp.Mul)):
            left = cls._eval_int_limit_expr(curr.this)
            right = cls._eval_int_limit_expr(curr.expression)
            if left is not None and right is not None:
                if isinstance(curr, exp.Add):
                    return left + right
                if isinstance(curr, exp.Sub):
                    return max(0, left - right)
                return left * right
        return None

    def _to_sql(self, expression: exp.Expr, quote: bool = True, **kwargs: t.Any) -> str:
        expression = self._normalize_schema_tmp_identifiers(expression)
        if expression.find(exp.Lock):
            expression = expression.copy()
            for select_node in expression.find_all(exp.Select):
                if select_node.args.get("locks"):
                    select_node.set("locks", None)
            logger.debug("[Doris] Removed unsupported FOR UPDATE lock from SELECT statement")
        if any(
            not isinstance(lim.expression, exp.Literal) for lim in expression.find_all(exp.Limit)
        ):
            expression = expression.copy()
            for lim in expression.find_all(exp.Limit):
                if not isinstance(lim.expression, exp.Literal):
                    folded = self._eval_int_limit_expr(lim.expression)
                    if folded is not None:
                        lim.set("expression", exp.Literal.number(folded))
        if any(
            isinstance(is_node.expression, exp.Boolean) for is_node in expression.find_all(exp.Is)
        ):
            expression = expression.copy()
            for is_node in list(expression.find_all(exp.Is)):
                if isinstance(is_node.expression, exp.Boolean):
                    eq_expr: exp.Expr = exp.EQ(
                        this=is_node.this.copy(), expression=is_node.expression.copy()
                    )
                    if isinstance(is_node.parent, exp.Not):
                        eq_expr = exp.Paren(this=eq_expr)
                    if is_node is expression:
                        expression = eq_expr
                    else:
                        is_node.replace(eq_expr)
        if expression.find(exp.AtTimeZone):
            expression = expression.copy()
            for at_tz in reversed(list(expression.find_all(exp.AtTimeZone))):
                zone_expr = at_tz.args.get("zone")
                if isinstance(zone_expr, exp.Literal) and zone_expr.is_string:
                    zone_val = zone_expr.this.strip()
                    to_tz: exp.Expr = (
                        exp.Literal.string("+00:00")
                        if zone_val.upper() == "UTC"
                        else exp.Literal.string(zone_val)
                    )
                elif zone_expr is not None:
                    to_tz = zone_expr.copy()
                else:
                    to_tz = exp.Literal.string("+00:00")
                convert_tz = exp.Anonymous(
                    this="CONVERT_TZ",
                    expressions=[
                        at_tz.this.copy(),
                        exp.SessionParameter(this=exp.var("time_zone")),
                        to_tz,
                    ],
                )
                if at_tz is expression:
                    expression = convert_tz
                else:
                    at_tz.replace(convert_tz)
        if expression.find(exp.TimeStrToTime):
            expression = expression.copy()
            for ts_node in reversed(list(expression.find_all(exp.TimeStrToTime))):
                if (
                    ts_node.args.get("zone")
                    and isinstance(ts_node.this, exp.Literal)
                    and ts_node.this.is_string
                    and "." in ts_node.this.this
                ):
                    cast_node = exp.cast(
                        ts_node.this.copy(),
                        exp.DataType.build(
                            "TIMESTAMPTZ",
                            expressions=[exp.DataTypeParam(this=exp.Literal.number(6))],
                        ),
                    )
                    if ts_node is expression:
                        expression = cast_node
                    else:
                        ts_node.replace(cast_node)
        if expression.find(exp.DataType):
            expression = expression.copy()
            for dtype in reversed(list(expression.find_all(exp.DataType))):
                if dtype.is_type("int128"):
                    replacement = exp.DataType.build("LARGEINT", udt=True)
                    if dtype is expression:
                        expression = replacement
                    else:
                        dtype.replace(replacement)
                elif (
                    dtype.is_type("timestamptz")
                    and not dtype.expressions
                    and isinstance(dtype.parent, (exp.Cast, exp.TryCast))
                    and isinstance(dtype.parent.this, exp.Literal)
                    and dtype.parent.this.is_string
                    and "." in dtype.parent.this.this
                ):
                    dtype.set("expressions", [exp.DataTypeParam(this=exp.Literal.number(6))])
                elif dtype.is_type("struct"):
                    new_field_sqls: t.List[str] = []
                    for field in dtype.expressions:
                        if isinstance(field, exp.ColumnDef) and field.args.get("kind"):
                            field_id = field.this.copy()
                            if quote and isinstance(field_id, exp.Identifier):
                                field_id.set("quoted", True)
                            field_name_sql = super()._to_sql(
                                field_id, quote=False, pretty=False, **kwargs
                            )
                            field_kind_sql = super()._to_sql(
                                field.args["kind"], quote=quote, pretty=False, **kwargs
                            )
                            new_field_sqls.append(f"{field_name_sql}: {field_kind_sql}")
                        else:
                            new_field_sqls.append(
                                super()._to_sql(field, quote=quote, pretty=False, **kwargs)
                            )
                    struct_replacement = exp.DataType(
                        this=exp.DataType.Type.USERDEFINED,
                        kind=f"STRUCT<{', '.join(new_field_sqls)}>",
                    )
                    if dtype is expression:
                        expression = struct_replacement
                    else:
                        dtype.replace(struct_replacement)
        return super()._to_sql(expression, quote=quote, **kwargs)

    def _build_distributed_by_exp(
        self,
        distributed_by: t.Any = None,
        clustered_by: t.Optional[t.List[exp.Expr]] = None,
        unique_key_cols: t.Optional[t.List[str]] = None,
        fallback_key_cols: t.Optional[t.List[str]] = None,
        top_level_buckets: t.Any = None,
    ) -> t.Optional[exp.DistributedByProperty]:
        default_buckets_expr = self._resolve_buckets(top_level_buckets)

        if distributed_by is not None:
            distributed_info: t.Dict[str, t.Any] = {}

            # Case 1: Function expression such as HASH(a, b) or RANDOM()
            if isinstance(distributed_by, (exp.Anonymous, exp.Func)) and not isinstance(
                distributed_by, (exp.Tuple, exp.Array, exp.Paren)
            ):
                kind_upper = (
                    "RANDOM"
                    if isinstance(distributed_by, exp.Rand)
                    else distributed_by.name.upper()
                )
                if kind_upper not in ("HASH", "RANDOM"):
                    raise SQLMeshError(
                        f"Invalid distributed_by kind: {kind_upper}. Expected HASH or RANDOM."
                    )
                cols = [
                    exp.to_column(c) for c in self._extract_column_names(distributed_by.expressions)
                ]
                if kind_upper == "RANDOM" and cols:
                    raise SQLMeshError(
                        "Doris DISTRIBUTED BY RANDOM does not accept column expressions."
                    )
                if kind_upper == "HASH" and not cols:
                    raise SQLMeshError(
                        "Doris DISTRIBUTED BY HASH requires at least one column expression."
                    )
                return exp.DistributedByProperty(
                    kind=exp.Var(this=kind_upper),
                    expressions=cols if cols else None,
                    buckets=default_buckets_expr or (exp.Literal.number(10) if cols else None),
                    order=None,
                )

            # Case 2: String shorthand or bare column/identifier such as "HASH(id) BUCKETS 10", "RANDOM BUCKETS AUTO", or id
            if isinstance(distributed_by, (str, exp.Column, exp.Identifier, exp.Var)) or (
                isinstance(distributed_by, exp.Literal)
                and getattr(distributed_by, "is_string", False)
            ):
                text = (
                    distributed_by.strip()
                    if isinstance(distributed_by, str)
                    else (
                        str(distributed_by.this).strip()
                        if isinstance(distributed_by, exp.Literal)
                        else distributed_by.name.strip()
                    )
                )
                m = re.match(
                    r"^(HASH|RANDOM)(?:\s*\(([^)]*)\)|\b)\s*(?:BUCKETS\s+(\S+))?$",
                    text,
                    flags=re.IGNORECASE,
                )
                if m:
                    kind_upper = m.group(1).upper()
                    cols_str = m.group(2)
                    b_str = m.group(3)
                    parsed_cols = (
                        [exp.to_column(c) for c in self._extract_column_names(cols_str)]
                        if cols_str
                        else None
                    )
                    if kind_upper == "RANDOM" and parsed_cols:
                        raise SQLMeshError(
                            "Doris DISTRIBUTED BY RANDOM does not accept column expressions."
                        )
                    if kind_upper == "HASH" and not parsed_cols:
                        raise SQLMeshError(
                            "Doris DISTRIBUTED BY HASH requires at least one column expression."
                        )
                    b_expr = self._resolve_buckets(b_str) if b_str else default_buckets_expr
                    return exp.DistributedByProperty(
                        kind=exp.Var(this=kind_upper),
                        expressions=parsed_cols,
                        buckets=b_expr,
                        order=None,
                    )
                cols = [exp.to_column(c) for c in self._extract_column_names(text)]
                if cols:
                    return exp.DistributedByProperty(
                        kind=exp.Var(this="HASH"),
                        expressions=cols,
                        buckets=default_buckets_expr or exp.Literal.number(10),
                        order=None,
                    )

            # Case 3: Tuple or Array (either key=value pairs or list of column names from dbt-doris)
            if isinstance(distributed_by, (exp.Tuple, exp.Array)):
                has_eq = any(isinstance(e, exp.EQ) for e in distributed_by.expressions)
                if has_eq:
                    for expr in distributed_by.expressions:
                        if isinstance(expr, exp.EQ):
                            key = (
                                (self._extract_str_value(expr.this) or "")
                                .strip('"')
                                .strip("'")
                                .strip("`")
                                .lower()
                            )
                            if isinstance(expr.expression, exp.Literal):
                                distributed_info[key] = expr.expression.this
                            elif isinstance(expr.expression, (exp.Array, exp.Tuple)):
                                distributed_info[key] = self._extract_column_names(expr.expression)
                            else:
                                distributed_info[key] = self._extract_str_value(expr.expression)
                else:
                    cols = [exp.to_column(c) for c in self._extract_column_names(distributed_by)]
                    if cols:
                        return exp.DistributedByProperty(
                            kind=exp.Var(this="HASH"),
                            expressions=cols,
                            buckets=default_buckets_expr or exp.Literal.number(10),
                            order=None,
                        )
            elif isinstance(distributed_by, exp.EQ) or (
                isinstance(distributed_by, exp.Paren) and isinstance(distributed_by.this, exp.EQ)
            ):
                expr = (
                    distributed_by.this if isinstance(distributed_by, exp.Paren) else distributed_by
                )
                key = (
                    (self._extract_str_value(expr.this) or "")
                    .strip('"')
                    .strip("'")
                    .strip("`")
                    .lower()
                )
                if isinstance(expr.expression, exp.Literal):
                    distributed_info[key] = expr.expression.this
                else:
                    distributed_info[key] = self._extract_str_value(expr.expression)
            elif isinstance(distributed_by, dict):
                distributed_info = distributed_by

            if distributed_info:
                raw_kind = distributed_info.get("kind")
                raw_random = distributed_info.get("random")
                expressions = (
                    distributed_info.get("expressions")
                    if distributed_info.get("expressions") is not None
                    else distributed_info.get("columns")
                )
                buckets = distributed_info.get("buckets", top_level_buckets)

                if raw_random is not None:
                    rand_str = (
                        str(self._extract_str_value(raw_random) or raw_random).strip().lower()
                    )
                    if rand_str in ("true", "1"):
                        if raw_kind is not None and str(raw_kind).strip().upper() != "RANDOM":
                            raise SQLMeshError(
                                f"Conflicting distributed_by configuration: kind='{raw_kind}' with random=true."
                            )
                        raw_kind = "RANDOM"
                if raw_kind is None and expressions is not None:
                    raw_kind = "HASH"

                if raw_kind:
                    kind_upper = str(raw_kind).strip().upper()
                    if kind_upper not in ("HASH", "RANDOM"):
                        raise SQLMeshError(
                            f"Invalid distributed_by kind: {kind_upper}. Expected HASH or RANDOM."
                        )
                    buckets_expr = self._resolve_buckets(buckets)

                    expressions_list = None
                    if expressions:
                        col_names = self._extract_column_names(expressions)
                        expressions_list = [exp.to_column(c) for c in col_names]

                    if kind_upper == "RANDOM" and expressions_list:
                        raise SQLMeshError(
                            "Doris DISTRIBUTED BY RANDOM does not accept column expressions."
                        )
                    if kind_upper == "HASH" and not expressions_list:
                        raise SQLMeshError(
                            "Doris DISTRIBUTED BY HASH requires at least one column expression."
                        )

                    return exp.DistributedByProperty(
                        kind=exp.Var(this=kind_upper),
                        expressions=expressions_list,
                        buckets=buckets_expr,
                        order=None,
                    )

        # Fallback 1: Top-level clustered_by (or physical_properties clustered_by)
        if clustered_by:
            col_names = [c for expr in clustered_by for c in self._extract_column_names(expr)]
            if unique_key_cols:
                strict_key_set = {k.lower() for k in unique_key_cols}
                not_in_key = [c for c in col_names if c.lower() not in strict_key_set]
                if not_in_key:
                    logger.warning(
                        f"[Doris] clustered_by columns {not_in_key} not in key columns {unique_key_cols}, falling back to key column distribution."
                    )
                    col_names = []
            cols = [exp.to_column(c) for c in col_names]
            if cols:
                return exp.DistributedByProperty(
                    kind=exp.Var(this="HASH"),
                    expressions=cols,
                    buckets=default_buckets_expr or exp.Literal.number(10),
                    order=None,
                )

        # Fallback 2: Default DISTRIBUTED BY HASH (<first_unique_key>) BUCKETS <buckets|10>
        if unique_key_cols:
            first_col = unique_key_cols[0]
            logger.info(
                f"[Doris] Adding default distributed_by using unique_key column: {first_col}"
            )
            return exp.DistributedByProperty(
                expressions=[exp.to_column(first_col)],
                kind="HASH",
                buckets=default_buckets_expr or exp.Literal.number(10),
            )

        # Fallback 3: Standalone buckets with duplicate_key/aggregate_key or RANDOM
        if default_buckets_expr is not None:
            if fallback_key_cols:
                return exp.DistributedByProperty(
                    kind=exp.Var(this="HASH"),
                    expressions=[exp.to_column(fallback_key_cols[0])],
                    buckets=default_buckets_expr,
                    order=None,
                )
            return exp.DistributedByProperty(
                kind=exp.Var(this="RANDOM"),
                expressions=None,
                buckets=default_buckets_expr,
                order=None,
            )

        return None

    def _get_temp_table(
        self, table: TableName, table_only: bool = False, quoted: bool = True
    ) -> exp.Table:
        """Returns the temp table expression, truncated to fit Doris's 64-char identifier limit."""
        table_copy = t.cast(exp.Table, exp.to_table(table).copy())
        table_copy.set(
            "this",
            exp.to_identifier(
                f"__temp_{table_copy.name[:48]}_{random_id(short=True)}", quoted=quoted
            ),
        )

        if table_only:
            table_copy.set("db", None)
            table_copy.set("catalog", None)

        return table_copy

    def _properties_to_expressions(self, properties: t.Dict[str, t.Any]) -> t.List[exp.Expr]:
        """Convert a dictionary of properties to a list of Doris PROPERTIES ('k'='v') expressions."""
        expressions: t.List[exp.Expr] = []
        skip_keys = {
            "build",
            "refresh",
            "refresh_trigger",
            "distributed_by",
            "clustered_by",
            "buckets",
            "partition_by",
            "partitioned_by",
            "partitions",
            "auto_partition",
            "unique_key",
            "duplicate_key",
            "aggregate_key",
            "table_type",
            "keys",
            "order_by",
            "cluster_by",
            "engine",
            "properties",
            "materialized_type",
            "source_table",
        }
        for key, value in properties.items():
            if key in skip_keys or value is None:
                continue
            raw_str = self._extract_str_value(value)
            val_str = raw_str if raw_str is not None else str(value)
            if key == "excluded_trigger_tables" and val_str:
                val_str = ",".join(
                    re.sub(r"(?i)^`?internal`?\.", "", part.strip())
                    for part in val_str.split(",")
                    if part.strip()
                )
            str_literal = exp.Literal.string(val_str)
            expressions.append(exp.Property(this=exp.Literal.string(str(key)), value=str_literal))
        return expressions
