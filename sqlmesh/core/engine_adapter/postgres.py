from __future__ import annotations

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
from sqlmesh.core.engine_adapter.shared import set_catalog

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import TableName
    from sqlmesh.core.engine_adapter._typing import DF, QueryOrDF

logger = logging.getLogger(__name__)


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
