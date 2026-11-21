from __future__ import annotations

import contextlib
import csv
import io
import logging
import re
import sys
import typing as t
from functools import cached_property, partial
from sqlglot import exp

from sqlmesh.core.engine_adapter.base_postgres import BasePostgresEngineAdapter
from sqlmesh.core.engine_adapter.mixins import (
    GetCurrentCatalogFromFunctionMixin,
    PandasNativeFetchDFSupportMixin,
    RowDiffMixin,
    logical_merge,
    GrantsFromInfoSchemaMixin,
)
from sqlmesh.core.engine_adapter.shared import SourceQuery, set_catalog
from sqlmesh.utils import random_id
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    import pandas as pd

    from sqlmesh.core._typing import TableName
    from sqlmesh.core.engine_adapter._typing import DF, Query, QueryOrDF

logger = logging.getLogger(__name__)

# pandas dtypes that can be bulk loaded with COPY, mapped to the type of the staging table column.
# The staging column types are derived from the DataFrame (not from the target table) and the final
# `SELECT` casts them to the target types, which mirrors how the generic `VALUES` implementation types
# literals by their Python value and then casts. In particular floats are staged as an unbounded
# NUMERIC, because the literals emitted by the `VALUES` implementation are NUMERIC and e.g. casting
# 2.5 to an integer rounds differently for NUMERIC (3) and DOUBLE PRECISION (2).
_COPY_DTYPE_TO_STAGING_TYPE: t.Dict[str, str] = {
    **{
        dtype: "BIGINT"
        for dtype in (
            "int8",
            "int16",
            "int32",
            "int64",
            "uint8",
            "uint16",
            "uint32",
            "Int8",
            "Int16",
            "Int32",
            "Int64",
            "UInt8",
            "UInt16",
            "UInt32",
        )
    },
    "float64": "NUMERIC",
    "Float64": "NUMERIC",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
}
# What pandas' `infer_dtype` reports for object / string columns that can be copied. Python numbers in an
# object column (e.g. seeds, where NaN is replaced with None) are staged as NUMERIC, which is lossless.
_COPY_INFERRED_TO_STAGING_TYPE: t.Dict[str, str] = {
    "string": "TEXT",
    "empty": "TEXT",  # only nulls
    "boolean": "BOOLEAN",
    "integer": "NUMERIC",
    "floating": "NUMERIC",
    "mixed-integer-float": "NUMERIC",
}


@set_catalog()
class PostgresEngineAdapter(
    BasePostgresEngineAdapter,
    PandasNativeFetchDFSupportMixin,
    GetCurrentCatalogFromFunctionMixin,
    RowDiffMixin,
    GrantsFromInfoSchemaMixin,
):
    DIALECT = "postgres"
    SUPPORTS_GRANTS = True
    SUPPORTS_INDEXES = True
    HAS_VIEW_BINDING = True
    CURRENT_CATALOG_EXPRESSION = exp.column("current_catalog")
    SUPPORTS_REPLACE_TABLE = False
    MAX_IDENTIFIER_LENGTH: t.Optional[int] = 63
    SUPPORTS_QUERY_EXECUTION_TRACKING = True
    GRANT_INFORMATION_SCHEMA_TABLE_NAME = "role_table_grants"
    CURRENT_USER_OR_ROLE_EXPRESSION: exp.Expr = exp.column("current_role")
    SUPPORTS_MULTIPLE_GRANT_PRINCIPALS = True
    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            # DECIMAL without precision is "up to 131072 digits before the decimal point; up to 16383 digits after the decimal point"
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(131072 + 16383, 16383), (0,)],
            exp.DataType.build("CHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("TIME", dialect=DIALECT).this: [(6,)],
            exp.DataType.build("TIMESTAMP", dialect=DIALECT).this: [(6,)],
        },
        "types_with_unlimited_length": {
            # all can ALTER to `TEXT`
            exp.DataType.build("TEXT", dialect=DIALECT).this: {
                exp.DataType.build("VARCHAR", dialect=DIALECT).this,
                exp.DataType.build("CHAR", dialect=DIALECT).this,
                exp.DataType.build("BPCHAR", dialect=DIALECT).this,
            },
            # all can ALTER to unparameterized `VARCHAR`
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: {
                exp.DataType.build("VARCHAR", dialect=DIALECT).this,
                exp.DataType.build("CHAR", dialect=DIALECT).this,
                exp.DataType.build("BPCHAR", dialect=DIALECT).this,
                exp.DataType.build("TEXT", dialect=DIALECT).this,
            },
            # parameterized `BPCHAR(n)` can ALTER to unparameterized `BPCHAR`
            exp.DataType.build("BPCHAR", dialect=DIALECT).this: {
                exp.DataType.build("BPCHAR", dialect=DIALECT).this
            },
        },
        "drop_cascade": True,
    }

    def _fetch_native_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> DF:
        """
        `read_sql_query` when using psycopg will result on a hanging transaction that must be committed

        https://github.com/pandas-dev/pandas/pull/42277
        """
        df = super()._fetch_native_df(query, quote_identifiers)
        if not self._connection_pool.is_transaction_active:
            self._connection_pool.commit()
        return df

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        """Bulk loads the DataFrame into a staging table with COPY instead of using `VALUES` batches.

        The generic implementation is used when the DataFrame cannot be loaded safely with COPY.
        """
        import pandas as pd

        assert isinstance(df, pd.DataFrame)

        # pandas truncates `na_rep` to 32 characters when writing float columns, so keep the marker short
        null_marker = f"__sqlmesh_null_{random_id(short=True)}__"
        # Same column selection and order as the generic implementation
        ordered_df = df[list(source_columns or target_columns_to_types)]
        staging_columns_to_types = (
            None
            if ordered_df.empty or not hasattr(self.cursor, "copy_expert")
            else self._get_copy_staging_columns_to_types(
                ordered_df, null_marker, target_columns_to_types
            )
        )
        if staging_columns_to_types is None:
            return super()._df_to_source_queries(
                df,
                target_columns_to_types,
                batch_size,
                target_table,
                source_columns=source_columns,
            )

        staging_table = self._get_temp_table(target_table or "pandas")
        committed_standalone = False

        def query_factory() -> Query:
            nonlocal committed_standalone
            # The factory can be called multiple times. The staging table is only loaded once, which
            # assumes that every call would have produced the same result.
            if not self.table_exists(staging_table):
                committed_standalone = False
                created_in_outer_tx = self._connection_pool.is_transaction_active
                try:
                    with self.transaction():
                        self.create_table(staging_table, staging_columns_to_types)
                        self._copy_df(staging_table, ordered_df, null_marker, batch_size)
                    if not created_in_outer_tx:
                        committed_standalone = True
                except Exception:
                    # Callers such as `replace_query` invoke the factory outside of `with source_query`,
                    # so `cleanup_func` won't run and a committed staging table would leak.
                    try:
                        self._drop_copy_staging_table(
                            staging_table, committed_standalone=committed_standalone
                        )
                    except Exception:
                        logger.warning("Failed to drop staging table %s", staging_table.sql())
                    raise
            return exp.select(
                *self._casted_columns(target_columns_to_types, source_columns=source_columns)
            ).from_(staging_table)

        return [
            SourceQuery(
                query_factory=query_factory,
                cleanup_func=lambda: self._drop_copy_staging_table(
                    staging_table, committed_standalone=committed_standalone
                ),
            )
        ]

    def _get_temp_table(
        self, table: TableName, table_only: bool = False, quoted: bool = True
    ) -> exp.Table:
        if self.MAX_IDENTIFIER_LENGTH is None:
            return super()._get_temp_table(table, table_only=table_only, quoted=quoted)

        table = t.cast(exp.Table, exp.to_table(table).copy())
        prefix, suffix = "__temp_", f"_{random_id(short=True)}"
        name = table.name
        prefix_bytes = len(prefix.encode("utf-8"))
        suffix_bytes = len(suffix.encode("utf-8"))
        max_bytes = self.MAX_IDENTIFIER_LENGTH
        total_bytes = prefix_bytes + len(name.encode("utf-8")) + suffix_bytes

        if total_bytes > max_bytes:
            budget = max(max_bytes - prefix_bytes - suffix_bytes, 0)
            if budget == 0:
                name = ""
            else:
                target_tail_bytes = budget // 2
                target_head_bytes = budget - target_tail_bytes

                head = []
                head_bytes = 0
                for ch in name:
                    ch_b = len(ch.encode("utf-8"))
                    if head_bytes + ch_b > target_head_bytes:
                        break
                    head.append(ch)
                    head_bytes += ch_b

                remaining_tail_budget = budget - head_bytes
                tail = []
                tail_bytes = 0
                for ch in reversed(name[len(head) :]):
                    ch_b = len(ch.encode("utf-8"))
                    if tail_bytes + ch_b > remaining_tail_budget:
                        break
                    tail.append(ch)
                    tail_bytes += ch_b

                name = "".join(head) + "".join(reversed(tail))

        table.set("this", exp.to_identifier(f"{prefix}{name}{suffix}", quoted=quoted))
        if table_only:
            table.set("db", None)
            table.set("catalog", None)
        return table

    def _check_identifier_length(self, expression: exp.Expr) -> None:
        if self.MAX_IDENTIFIER_LENGTH is None or not isinstance(expression, exp.DDL):
            return

        for identifier in expression.find_all(exp.Identifier):
            name = identifier.name
            name_bytes = len(name.encode("utf-8"))
            if name_bytes > self.MAX_IDENTIFIER_LENGTH:
                raise SQLMeshError(
                    f"Identifier name '{name}' (length {name_bytes} bytes) exceeds {self.dialect.capitalize()}'s max identifier limit of {self.MAX_IDENTIFIER_LENGTH} bytes"
                )

    def _drop_copy_staging_table(
        self, staging_table: exp.Table, *, committed_standalone: bool = False
    ) -> None:
        exc_propagating = sys.exc_info()[0] is not None
        try:
            self.drop_table(staging_table)
        except Exception as ex:
            sqlstate = getattr(ex, "pgcode", None) or getattr(ex, "sqlstate", None)
            if (
                sqlstate == "25P02"
                and self._connection_pool.is_transaction_active
                and not committed_standalone
            ):
                # The staging table was created in the active (aborted) outer transaction, so its
                # rollback removes it without masking the caller's error.
                logger.debug("Skipped dropping %s from an aborted transaction", staging_table.sql())
                return
            if sqlstate == "25P02" and committed_standalone:
                # The staging table was already committed in its own transaction before a subsequent
                # statement aborted the connection's transaction, so rollback alone will not drop it.
                was_in_tx = self._connection_pool.is_transaction_active
                try:
                    self._connection_pool.rollback()
                    with self.transaction():
                        self.drop_table(staging_table)
                    if was_in_tx and not exc_propagating:
                        self._connection_pool.begin()
                    return
                except Exception:
                    if exc_propagating:
                        logger.warning(
                            "Failed to drop staging table %s after rolling back aborted transaction",
                            staging_table.sql(),
                            exc_info=True,
                        )
                        return
                    raise
            if exc_propagating:
                logger.warning(
                    "Failed to drop staging table %s",
                    staging_table.sql(),
                    exc_info=True,
                )
                return
            raise

    @staticmethod
    def _is_oid_or_reg_type(data_type: exp.DataType) -> bool:
        type_str = str(data_type.this).lower()
        sql_str = data_type.sql("postgres").lower()
        return (
            type_str
            in (
                "oid",
                "regclass",
                "regtype",
                "regproc",
                "regprocedure",
                "regoper",
                "regoperator",
                "regnamespace",
                "regrole",
                "regconfig",
                "regdictionary",
                "regcollation",
            )
            or sql_str == "oid"
            or sql_str.startswith("reg")
        )

    @classmethod
    def _get_copy_staging_columns_to_types(
        cls,
        df: pd.DataFrame,
        null_marker: str,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> t.Optional[t.Dict[str, exp.DataType]]:
        """Returns the staging table column types for the DataFrame, or None if it can't be loaded with COPY.

        None is returned when a column has a dtype or values that wouldn't be read back by COPY exactly
        as the generic `VALUES` implementation would write them, e.g. dicts, bytes, Decimals, dates or
        categoricals, or when a string would be misread by the CSV parser, or when the staging type is
        incompatible with the target column type.
        """
        import pandas as pd

        if target_columns_to_types is None:
            return None

        staging_columns_to_types: t.Dict[str, exp.DataType] = {}
        for column, series in df.items():
            col_name = str(column)
            target_type = target_columns_to_types.get(col_name)
            if target_type is None:
                return None
            if not isinstance(target_type, exp.DataType):
                target_type = exp.DataType.build(target_type, dialect=cls.DIALECT)
            if target_type.is_type(exp.DataType.Type.UNKNOWN):
                return None

            dtype = series.dtype
            staging_type: t.Optional[str]
            if isinstance(dtype, pd.DatetimeTZDtype):
                staging_type = "TIMESTAMPTZ"
            elif pd.api.types.is_datetime64_dtype(dtype):
                staging_type = "TIMESTAMP"
            elif str(dtype) in _COPY_DTYPE_TO_STAGING_TYPE:
                staging_type = _COPY_DTYPE_TO_STAGING_TYPE[str(dtype)]
                if staging_type == "BIGINT" and target_type.is_type(exp.DataType.Type.BOOLEAN):
                    return None
                if staging_type == "NUMERIC":
                    if target_type.is_type(
                        exp.DataType.Type.BOOLEAN, exp.DataType.Type.BIT
                    ) or cls._is_oid_or_reg_type(target_type):
                        return None
            elif dtype == object or isinstance(dtype, pd.StringDtype):
                inferred = pd.api.types.infer_dtype(series, skipna=True)
                if inferred == "integer":
                    if target_type.is_type(exp.DataType.Type.BOOLEAN):
                        return None
                    non_nulls = series.dropna()
                    try:
                        fits_bigint = bool(
                            (
                                (non_nulls >= -9223372036854775808)
                                & (non_nulls <= 9223372036854775807)
                            ).all()
                        )
                    except Exception:
                        fits_bigint = False

                    if fits_bigint:
                        staging_type = "BIGINT"
                    else:
                        staging_type = "NUMERIC"
                        if target_type.is_type(exp.DataType.Type.BIT) or cls._is_oid_or_reg_type(
                            target_type
                        ):
                            return None
                else:
                    staging_type = _COPY_INFERRED_TO_STAGING_TYPE.get(inferred)
                    if staging_type == "TEXT":
                        if series.eq(null_marker).any():
                            return None
                        # On PostgreSQL <= 17 an unquoted `\.` line ends the data, which happens if it is the only column
                        if len(df.columns) == 1 and series.eq("\\.").any():
                            return None
                    elif staging_type == "NUMERIC":
                        if target_type.is_type(
                            exp.DataType.Type.BOOLEAN, exp.DataType.Type.BIT
                        ) or cls._is_oid_or_reg_type(target_type):
                            return None
            else:
                staging_type = None
            if staging_type is None:
                return None
            if staging_type != "TEXT" and target_type.is_type(
                exp.DataType.Type.JSON, exp.DataType.Type.JSONB
            ):
                return None
            staging_columns_to_types[col_name] = exp.DataType.build(
                staging_type, dialect=PostgresEngineAdapter.DIALECT
            )
        return staging_columns_to_types

    def _copy_df(
        self, table: exp.Table, df: pd.DataFrame, null_marker: str, batch_size: int
    ) -> None:
        """Loads the DataFrame into the table with `COPY ... FROM STDIN` in chunks of `batch_size` rows.

        This is the only place that calls the driver's COPY API (`psycopg2`'s `copy_expert`).
        The copy runs on the adapter's cursor so it is part of the current transaction.
        """
        columns = ", ".join(self._to_sql(exp.column(c, quoted=True)) for c in df.columns)
        copy_sql = (
            f"COPY {self._to_sql(table)} ({columns}) "
            f"FROM STDIN WITH (FORMAT csv, NULL '{null_marker}')"
        )
        chunk_size = batch_size or max(len(df.index), 1)
        for start in range(0, len(df.index), chunk_size):
            buffer = io.StringIO()
            df.iloc[start : start + chunk_size].to_csv(
                buffer,
                header=False,
                index=False,
                na_rep=null_marker,
                quoting=csv.QUOTE_MINIMAL,
                lineterminator="\r\n",
            )
            buffer.seek(0)
            # A text buffer is encoded by the driver with the connection's client_encoding
            self.cursor.copy_expert(copy_sql, buffer)

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
        import pandas as pd

        with self.transaction(condition=isinstance(query_or_df, pd.DataFrame)):
            return super().replace_query(
                table_name,
                query_or_df,
                target_columns_to_types=target_columns_to_types,
                table_description=table_description,
                column_descriptions=column_descriptions,
                source_columns=source_columns,
                supports_replace_table_override=supports_replace_table_override,
                **kwargs,
            )

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
        import pandas as pd

        with self.transaction(condition=isinstance(query_or_df, pd.DataFrame)):
            return super().ctas(
                table_name,
                query_or_df,
                target_columns_to_types=target_columns_to_types,
                exists=exists,
                table_description=table_description,
                column_descriptions=column_descriptions,
                source_columns=source_columns,
                **kwargs,
            )

    def _scd_type_2(
        self,
        target_table: TableName,
        source_table: QueryOrDF,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> None:
        import pandas as pd

        with self.transaction(condition=isinstance(source_table, pd.DataFrame)):
            super()._scd_type_2(target_table, source_table, *args, **kwargs)

    @contextlib.contextmanager
    def _comment_savepoint(self) -> t.Iterator[None]:
        if (
            self._connection_pool.is_transaction_active is not True
            or not self.SUPPORTS_TRANSACTIONS
        ):
            yield
            return

        savepoint = "_sqlmesh_comment"
        self.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except Exception:
            try:
                self.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                logger.debug("Failed to roll back savepoint %s", savepoint, exc_info=True)
            raise
        else:
            self.execute(f"RELEASE SAVEPOINT {savepoint}")

    def _create_table_comment(
        self, table_name: TableName, table_comment: str, table_kind: str = "TABLE"
    ) -> None:
        table = exp.to_table(table_name)
        try:
            with self._comment_savepoint():
                self.execute(self._build_create_comment_table_exp(table, table_comment, table_kind))
        except Exception:
            logger.warning(
                f"Table comment for '{table.alias_or_name}' not registered - this may be due to limited permissions",
                exc_info=True,
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
                with self._comment_savepoint():
                    self.execute(
                        self._build_create_comment_column_exp(table, col, comment, table_kind)
                    )
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
        self.execute(
            exp.Create(
                this=exp.Schema(
                    this=exp.to_table(target_table_name),
                    expressions=[
                        exp.LikeProperty(
                            this=exp.to_table(source_table_name),
                            expressions=[exp.Property(this="INCLUDING", value=exp.Var(this="ALL"))],
                        )
                    ],
                ),
                kind="TABLE",
                exists=exists,
            )
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
        import pandas as pd

        # Merge isn't supported until Postgres 15
        major, minor = self.server_version
        merge_impl = super().merge if major >= 15 else partial(logical_merge, self)
        with self.transaction(condition=isinstance(source_table, pd.DataFrame)):
            merge_impl(  # type: ignore
                target_table,
                source_table,
                target_columns_to_types,
                unique_key,
                when_matched=when_matched,
                merge_filter=merge_filter,
                source_columns=source_columns,
            )

    @cached_property
    def server_version(self) -> t.Tuple[int, int]:
        """Lazily fetch and cache major and minor server version"""
        if result := self.fetchone("SHOW server_version"):
            server_version, *_ = result
            match = re.search(r"(\d+)\.(\d+)", server_version)
            if match:
                return int(match.group(1)), int(match.group(2))
        return 0, 0
