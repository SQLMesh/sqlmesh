from __future__ import annotations

import csv
import io
import logging
import re
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
            else self._get_copy_staging_columns_to_types(ordered_df, null_marker)
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

        def query_factory() -> Query:
            # The factory can be called multiple times. The staging table is only loaded once, which
            # assumes that every call would have produced the same result.
            if not self.table_exists(staging_table):
                try:
                    self.create_table(staging_table, staging_columns_to_types)
                    self._copy_df(staging_table, ordered_df, null_marker, batch_size)
                except Exception:
                    # Callers such as `replace_query` invoke the factory outside of `with source_query`,
                    # so `cleanup_func` won't run and a committed staging table would leak.
                    try:
                        self._drop_copy_staging_table(staging_table)
                    except Exception:
                        logger.warning("Failed to drop staging table %s", staging_table.sql())
                    raise
            return exp.select(
                *self._casted_columns(target_columns_to_types, source_columns=source_columns)
            ).from_(staging_table)

        return [
            SourceQuery(
                query_factory=query_factory,
                cleanup_func=lambda: self._drop_copy_staging_table(staging_table),
            )
        ]

    def _drop_copy_staging_table(self, staging_table: exp.Table) -> None:
        try:
            self.drop_table(staging_table)
        except Exception as ex:
            # The cleanup runs while an exception is propagating. If it left the transaction aborted
            # the DROP fails with "current transaction is aborted", which would hide the original
            # error. The staging table was created in that transaction, so its rollback removes it.
            sqlstate = getattr(ex, "pgcode", None) or getattr(ex, "sqlstate", None)
            if sqlstate != "25P02" or not self._connection_pool.is_transaction_active:
                raise
            logger.debug("Skipped dropping %s from an aborted transaction", staging_table.sql())

    @staticmethod
    def _get_copy_staging_columns_to_types(
        df: pd.DataFrame, null_marker: str
    ) -> t.Optional[t.Dict[str, exp.DataType]]:
        """Returns the staging table column types for the DataFrame, or None if it can't be loaded with COPY.

        None is returned when a column has a dtype or values that wouldn't be read back by COPY exactly
        as the generic `VALUES` implementation would write them, e.g. dicts, bytes, Decimals, dates or
        categoricals, or when a string would be misread by the CSV parser.
        """
        import pandas as pd

        staging_columns_to_types: t.Dict[str, exp.DataType] = {}
        for column, series in df.items():
            dtype = series.dtype
            staging_type: t.Optional[str]
            if isinstance(dtype, pd.DatetimeTZDtype):
                staging_type = "TIMESTAMPTZ"
            elif pd.api.types.is_datetime64_dtype(dtype):
                staging_type = "TIMESTAMP"
            elif str(dtype) in _COPY_DTYPE_TO_STAGING_TYPE:
                staging_type = _COPY_DTYPE_TO_STAGING_TYPE[str(dtype)]
            elif dtype == object or isinstance(dtype, pd.StringDtype):
                staging_type = _COPY_INFERRED_TO_STAGING_TYPE.get(
                    pd.api.types.infer_dtype(series, skipna=True)
                )
                if staging_type == "TEXT":
                    if series.eq(null_marker).any():
                        return None
                    # On PostgreSQL <= 17 an unquoted `\.` line ends the data, which happens if it is the only column
                    if len(df.columns) == 1 and series.eq("\\.").any():
                        return None
            else:
                staging_type = None
            if staging_type is None:
                return None
            staging_columns_to_types[str(column)] = exp.DataType.build(
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
                lineterminator="\n",
            )
            buffer.seek(0)
            # A text buffer is encoded by the driver with the connection's client_encoding
            self.cursor.copy_expert(copy_sql, buffer)

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
        # Merge isn't supported until Postgres 15
        major, minor = self.server_version
        merge_impl = super().merge if major >= 15 else partial(logical_merge, self)
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
