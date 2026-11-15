from __future__ import annotations

import typing as t
from functools import cached_property, partial
from sqlglot import exp
from pathlib import Path
from packaging import version

from sqlmesh.core.engine_adapter.base import MERGE_TARGET_ALIAS
from sqlmesh.core.engine_adapter.mixins import (
    GetCurrentCatalogFromFunctionMixin,
    RowDiffMixin,
    logical_merge,
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

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter._typing import DF, Query, QueryOrDF


@set_catalog(override_mapping={"_get_data_objects": CatalogSupport.REQUIRES_SET_CATALOG})
class DuckDBEngineAdapter(GetCurrentCatalogFromFunctionMixin, RowDiffMixin):
    DIALECT = "duckdb"
    SUPPORTS_TRANSACTIONS = False
    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(18, 3), (0,)],
        },
    }
    COMMENT_CREATION_TABLE = CommentCreationTable.COMMENT_COMMAND_ONLY
    COMMENT_CREATION_VIEW = CommentCreationView.COMMENT_COMMAND_ONLY
    SUPPORTS_CREATE_DROP_CATALOG = True
    SUPPORTED_DROP_CASCADE_OBJECT_KINDS = ["SCHEMA", "TABLE", "VIEW"]
    # MERGE INTO was added in DuckDB 1.4.0
    MIN_NATIVE_MERGE_VERSION = version.parse("1.4.0")
    # Catalog types whose storage extension supports MERGE INTO. Other attached catalogs
    # (postgres, sqlite, mysql, ...) fall back to DELETE + INSERT.
    NATIVE_MERGE_CATALOG_TYPES = {"duckdb", "ducklake", "motherduck"}

    @property
    def catalog_support(self) -> CatalogSupport:
        return CatalogSupport.FULL_SUPPORT

    def set_current_catalog(self, catalog: str) -> None:
        """Sets the catalog name of the current connection."""
        self.execute(exp.Use(this=exp.to_identifier(catalog)))

    def _create_catalog(self, catalog_name: exp.Identifier) -> None:
        if not self._is_motherduck:
            db_filename = f"{catalog_name.output_name}.db"
            self.execute(
                exp.Attach(
                    this=exp.alias_(exp.Literal.string(db_filename), catalog_name), exists=True
                )
            )
        else:
            self.execute(
                exp.Create(this=exp.Table(this=catalog_name), kind="DATABASE", exists=True)
            )

    def _drop_catalog(self, catalog_name: exp.Identifier) -> None:
        if not self._is_motherduck:
            db_file_path = Path(f"{catalog_name.output_name}.db")
            self.execute(exp.Detach(this=catalog_name, exists=True))
            if db_file_path.exists():
                db_file_path.unlink()
        else:
            self.execute(
                exp.Drop(
                    this=exp.Table(this=catalog_name), kind="DATABASE", cascade=True, exists=True
                )
            )

    @cached_property
    def _duckdb_supports_merge(self) -> bool:
        import duckdb

        return version.parse(duckdb.__version__) >= self.MIN_NATIVE_MERGE_VERSION

    def _supports_native_merge(self, target_table: TableName) -> bool:
        if not self._duckdb_supports_merge:
            return False
        catalog = (
            exp.to_table(target_table, dialect=self.dialect).catalog or self.get_current_catalog()
        )
        # Errors from the catalog lookup propagate on purpose; only a None/unknown type falls back.
        catalog_type = (self._get_catalog_type(catalog) or "").lower()
        return catalog_type in self.NATIVE_MERGE_CATALOG_TYPES

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
        merge_impl = (
            super().merge
            if self._supports_native_merge(target_table)
            else partial(logical_merge, self)
        )
        merge_impl(  # type: ignore
            target_table,
            source_table,
            target_columns_to_types,
            unique_key,
            when_matched=when_matched,
            merge_filter=merge_filter,
            source_columns=source_columns,
        )

    def _prepare_merge_source_query(
        self,
        query: Query,
        target_table: TableName,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        unique_key: t.Optional[t.Sequence[exp.Expr]] = None,
        when_matched: t.Optional[exp.Whens] = None,
        merge_filter: t.Optional[exp.Expr] = None,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> Query:
        if target_columns_to_types is None:
            try:
                target_columns_to_types = self.columns(target_table)
            except Exception:
                target_columns_to_types = None

        unique_key = unique_key or []

        if not target_columns_to_types or all(
            not dtype or dtype.is_type(exp.DataType.Type.UNKNOWN)
            for dtype in target_columns_to_types.values()
        ):
            return exp.select("*").distinct(*unique_key).from_(query.subquery("_src", copy=False))  # type: ignore

        source_columns_set = (
            {s.lower() for s in source_columns} if source_columns is not None else None
        )
        target_cols_to_project = (
            [col for col in target_columns_to_types if col.lower() in source_columns_set]
            if source_columns_set is not None
            else list(target_columns_to_types)
        )
        projected_target_cols = {col.lower(): col for col in target_cols_to_project}

        projections: t.List[exp.Expr] = []
        for col in target_cols_to_project:
            dtype = target_columns_to_types[col]
            if dtype and not dtype.is_type(exp.DataType.Type.UNKNOWN):
                projections.append(exp.alias_(exp.cast(exp.column(col), to=dtype), col, copy=False))
            else:
                projections.append(exp.column(col))

        seen_extra: t.Set[str] = set()
        extra_cols: t.List[str] = []
        target_names = {
            MERGE_TARGET_ALIAS,
            exp.to_table(target_table, dialect=self.dialect).name.upper(),
        }
        for expr in [when_matched, merge_filter, *unique_key]:
            if expr:
                for column_expr in expr.find_all(exp.Column):
                    if column_expr.table and column_expr.table.upper() in target_names:
                        continue
                    col_name = column_expr.name
                    if (
                        col_name.lower() not in projected_target_cols
                        and col_name.lower() not in seen_extra
                    ):
                        seen_extra.add(col_name.lower())
                        extra_cols.append(col_name)

        inner_query = query.this if isinstance(query, exp.Subquery) else query
        if isinstance(inner_query, exp.Select):
            for name in inner_query.named_selects:
                if (
                    name != "*"
                    and name.lower() not in projected_target_cols
                    and name.lower() not in seen_extra
                ):
                    seen_extra.add(name.lower())
                    extra_cols.append(name)

        for extra in extra_cols:
            projections.append(exp.column(extra))

        coerced = exp.select(*projections).from_(query.subquery("_raw_src", copy=False), copy=False)
        return exp.select("*").distinct(*unique_key).from_(coerced.subquery("_src", copy=False))  # type: ignore

    def _merge(
        self,
        target_table: TableName,
        query: Query,
        on: exp.Expr,
        whens: exp.Whens,
    ) -> None:
        # DuckDB rejects a qualified column on the left-hand side of UPDATE SET and in the INSERT
        # column list. Conditions and right-hand sides may keep the qualifier.
        # TODO: sqlglot's DuckDB generator doesn't apply `merge_without_target_sql` yet. Remove this
        # once the pinned sqlglot version does.
        whens = whens.copy()
        target_names = {
            MERGE_TARGET_ALIAS,
            exp.to_table(target_table, dialect=self.dialect).name.upper(),
        }
        for when in whens.expressions:
            then = when.args.get("then")
            targets: t.List[exp.Expr] = []
            if isinstance(then, exp.Update):
                targets = [e.this for e in then.expressions if isinstance(e, exp.EQ)]
            elif isinstance(then, exp.Insert) and isinstance(then.this, exp.Tuple):
                targets = then.this.expressions
            for column in targets:
                if (
                    isinstance(column, exp.Column)
                    and column.table
                    and column.table.upper() in target_names
                ):
                    column.set("table", None)
        super()._merge(target_table, query, on, whens)

    def _df_to_source_queries(
        self,
        df: DF,
        target_columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int,
        target_table: TableName,
        source_columns: t.Optional[t.List[str]] = None,
    ) -> t.List[SourceQuery]:
        temp_table = self._get_temp_table(target_table)
        temp_table_sql = (
            exp.select(*self._casted_columns(target_columns_to_types, source_columns))
            .from_("df")
            .sql(dialect=self.dialect)
        )
        self.cursor.sql(f"CREATE TABLE {temp_table} AS {temp_table_sql}")
        return [
            SourceQuery(
                query_factory=lambda: self._select_columns(target_columns_to_types).from_(
                    temp_table
                ),  # type: ignore
                cleanup_func=lambda: self.drop_table(temp_table),
            )
        ]

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and optionally catalog.
        """
        catalog = self.get_current_catalog()

        if isinstance(schema_name, exp.Table):
            # Ensures we don't generate identifier quotes
            schema_name = ".".join(part.name for part in schema_name.parts)

        query = (
            exp.select(
                exp.column("table_name").as_("name"),
                exp.column("table_schema").as_("schema"),
                exp.case(exp.column("table_type"))
                .when(
                    exp.Literal.string("BASE TABLE"),
                    exp.Literal.string("table"),
                )
                .when(
                    exp.Literal.string("VIEW"),
                    exp.Literal.string("view"),
                )
                .when(
                    exp.Literal.string("LOCAL TEMPORARY"),
                    exp.Literal.string("table"),
                )
                .as_("type"),
            )
            .from_(exp.to_table("system.information_schema.tables"))
            .where(
                exp.column("table_catalog").eq(catalog), exp.column("table_schema").eq(schema_name)
            )
        )
        if object_names:
            query = query.where(exp.column("table_name").isin(*object_names))
        df = self.fetchdf(query)
        return [
            DataObject(
                catalog=catalog,  # type: ignore
                schema=row.schema,  # type: ignore
                name=row.name,  # type: ignore
                type=DataObjectType.from_str(row.type),  # type: ignore
            )
            for row in df.itertuples()
        ]

    def _normalize_decimal_value(self, col: exp.Expr, precision: int) -> exp.Expr:
        """
        duckdb truncates instead of rounding when casting to decimal.

        other databases: select cast(3.14159 as decimal(38,3)) -> 3.142
        duckdb: select cast(3.14159 as decimal(38,3)) -> 3.141

        however, we can get the behaviour of other databases by casting to double first:
        select cast(cast(3.14159 as double) as decimal(38, 3)) -> 3.142
        """
        return exp.cast(
            exp.cast(col, "DOUBLE"),
            f"DECIMAL(38, {precision})",
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
        partitioned_by_exps = None
        if self._get_catalog_type(self.get_current_catalog()) == "ducklake":
            partitioned_by_exps = kwargs.pop("partitioned_by", None)

        super()._create_table(
            table_name_or_schema,
            expression,
            exists,
            replace,
            target_columns_to_types,
            table_description,
            column_descriptions,
            table_kind,
            track_rows_processed=track_rows_processed,
            **kwargs,
        )

        if partitioned_by_exps:
            # Schema object contains column definitions, so we extract Table
            table_name = (
                table_name_or_schema.this
                if isinstance(table_name_or_schema, exp.Schema)
                else table_name_or_schema
            )
            table_name_str = (
                table_name.sql(dialect=self.dialect)
                if isinstance(table_name, exp.Table)
                else table_name
            )
            partitioned_by_str = ", ".join(
                expr.sql(dialect=self.dialect) for expr in partitioned_by_exps
            )
            self.execute(f"ALTER TABLE {table_name_str} SET PARTITIONED BY ({partitioned_by_str});")

    def _drop_object(
        self,
        name: TableName | SchemaName,
        exists: bool = True,
        kind: str = "TABLE",
        cascade: bool = False,
        **drop_args: t.Any,
    ) -> None:
        # DuckLake catalogs do not implement DROP TABLE / DROP VIEW ... CASCADE and raise
        # "Cascade Drop not supported in DuckLake". Views in DuckDB are late-binding, so
        # dropping the underlying table without CASCADE is safe there.
        if cascade and kind.upper() in ("TABLE", "VIEW"):
            catalog = exp.to_table(name).catalog or self.get_current_catalog()
            if self._get_catalog_type(catalog) == "ducklake":
                cascade = False

        super()._drop_object(name=name, exists=exists, kind=kind, cascade=cascade, **drop_args)

    def _get_catalog_type(self, catalog: t.Optional[str]) -> t.Optional[str]:
        """Returns the type of the given catalog (e.g. 'duckdb', 'ducklake') as reported by duckdb_databases()."""
        if not catalog:
            return None
        catalog_type_tuple = self.fetchone(
            exp.select("type")
            .from_("duckdb_databases()")
            .where(
                exp.func("LOWER", exp.column("database_name")).eq(
                    exp.func("LOWER", exp.Literal.string(catalog))
                )
            )
        )
        return catalog_type_tuple[0] if catalog_type_tuple else None

    @property
    def _is_motherduck(self) -> bool:
        return self._extra_config.get("is_motherduck", False)
