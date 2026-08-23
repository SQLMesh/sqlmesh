from __future__ import annotations

import logging
import typing as t

from sqlglot import exp
from sqlglot.helper import ensure_list

from sqlmesh.core.dialect import to_schema
from sqlmesh.core.engine_adapter.base import MERGE_SOURCE_ALIAS, MERGE_TARGET_ALIAS
from sqlmesh.core.engine_adapter.base_postgres import BasePostgresEngineAdapter
from sqlmesh.core.engine_adapter.mixins import (
    GetCurrentCatalogFromFunctionMixin,
    NonTransactionalTruncateMixin,
    VarcharSizeWorkaroundMixin,
    RowDiffMixin,
    logical_merge,
    GrantsFromInfoSchemaMixin,
)
from sqlmesh.core.engine_adapter.shared import (
    CommentCreationView,
    DataObject,
    DataObjectType,
    SourceQuery,
    set_catalog,
)
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    import pandas as pd

    from sqlmesh.core._typing import SchemaName, TableName
    from sqlmesh.core.engine_adapter.base import QueryOrDF, Query
    from sqlmesh.core.node import IntervalUnit

logger = logging.getLogger(__name__)


@set_catalog()
class RedshiftEngineAdapter(
    BasePostgresEngineAdapter,
    GetCurrentCatalogFromFunctionMixin,
    NonTransactionalTruncateMixin,
    VarcharSizeWorkaroundMixin,
    RowDiffMixin,
    GrantsFromInfoSchemaMixin,
):
    DIALECT = "redshift"
    CURRENT_CATALOG_EXPRESSION = exp.func("current_database")
    # Redshift doesn't support comments for VIEWs WITH NO SCHEMA BINDING (which we always use)
    COMMENT_CREATION_VIEW = CommentCreationView.UNSUPPORTED
    SUPPORTS_REPLACE_TABLE = False
    SUPPORTS_GRANTS = True
    SUPPORTS_MULTIPLE_GRANT_PRINCIPALS = True

    SCHEMA_DIFFER_KWARGS = {
        "parameterized_type_defaults": {
            exp.DataType.build("VARBYTE", dialect=DIALECT).this: [(64000,)],
            exp.DataType.build("DECIMAL", dialect=DIALECT).this: [(18, 0), (0,)],
            exp.DataType.build("CHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: [(256,)],
            exp.DataType.build("NCHAR", dialect=DIALECT).this: [(1,)],
            exp.DataType.build("NVARCHAR", dialect=DIALECT).this: [(256,)],
        },
        "max_parameter_length": {
            exp.DataType.build("CHAR", dialect=DIALECT).this: 4096,
            exp.DataType.build("VARCHAR", dialect=DIALECT).this: 65535,
        },
        "precision_increase_allowed_types": {exp.DataType.build("VARCHAR", dialect=DIALECT).this},
        "drop_cascade": True,
    }
    VARIABLE_LENGTH_DATA_TYPES = {
        "char",
        "character",
        "nchar",
        "varchar",
        "character varying",
        "nvarchar",
        "varbyte",
        "varbinary",
        "binary varying",
    }

    def columns(
        self,
        table_name: TableName,
        include_pseudo_columns: bool = True,
    ) -> t.Dict[str, exp.DataType]:
        table = exp.to_table(table_name)

        sql = (
            exp.select(
                "column_name",
                "data_type",
                "character_maximum_length",
                "numeric_precision",
                "numeric_scale",
            )
            .from_("svv_columns")  # Includes late-binding views
            .where(exp.column("table_name").eq(table.alias_or_name))
        )
        if table.args.get("db"):
            sql = sql.where(exp.column("table_schema").eq(table.args["db"].name))

        columns_raw = self.fetchall(sql, quote_identifiers=True)

        def build_var_length_col(
            column_name: str,
            data_type: str,
            character_maximum_length: t.Optional[int] = None,
            numeric_precision: t.Optional[int] = None,
            numeric_scale: t.Optional[int] = None,
        ) -> tuple:
            data_type = data_type.lower()
            if (
                data_type in self.VARIABLE_LENGTH_DATA_TYPES
                and character_maximum_length is not None
            ):
                return (column_name, f"{data_type}({character_maximum_length})")
            if data_type in ("decimal", "numeric"):
                return (column_name, f"{data_type}({numeric_precision}, {numeric_scale})")

            return (column_name, data_type)

        columns = [build_var_length_col(*row) for row in columns_raw]

        return {
            column_name: exp.DataType.build(data_type, dialect=self.dialect)
            for column_name, data_type in columns
        }

    @property
    def enable_merge(self) -> bool:
        # Redshift supports the MERGE operation but we use the logical merge
        # unless the user has opted in by setting enable_merge in the connection.
        return bool(self._extra_config.get("enable_merge"))

    @property
    def cursor(self) -> t.Any:
        # Redshift by default uses a `format` paramstyle that has issues when we try to write our snapshot
        # data to snapshot table. There doesn't seem to be a way to disable parameter overriding so we just
        # set it to `qmark` since that doesn't cause issues.
        cursor = self._connection_pool.get_cursor()
        cursor.paramstyle = "qmark"
        return cursor

    def _fetch_native_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> pd.DataFrame:
        """Fetches a Pandas DataFrame from the cursor"""
        import pandas as pd

        self.execute(query, quote_identifiers=quote_identifiers)

        # We manually build the `DataFrame` here because the driver's `fetch_dataframe`
        # method does not respect the active case-sensitivity configuration.
        #
        # Context: https://github.com/aws/amazon-redshift-python-driver/issues/238
        fetcheddata = self.cursor.fetchall()

        try:
            columns = [column[0] for column in self.cursor.description]
        except Exception:
            columns = None
            logging.warning(
                "No row description was found, pandas dataframe will be missing column labels."
            )

        result = [tuple(row) for row in fetcheddata]
        return pd.DataFrame(result, columns=columns)

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
        """
        Redshift doesn't support `CREATE TABLE IF NOT EXISTS AS...` but does support `CREATE TABLE AS...` so
        we check if the exists check exists and if not then we can use the base implementation. Otherwise we
        manually check if it exists and if it does then this is a no-op anyways so we return and if it doesn't
        then we run the query with exists set to False since we just confirmed it doesn't exist.
        """
        if not exists:
            return super()._create_table_from_source_queries(
                table_name,
                source_queries,
                target_columns_to_types,
                exists,
                table_description=table_description,
                column_descriptions=column_descriptions,
                **kwargs,
            )
        if self.table_exists(table_name):
            return
        super()._create_table_from_source_queries(
            table_name,
            source_queries,
            exists=False,
            table_description=table_description,
            column_descriptions=column_descriptions,
            **kwargs,
        )

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
        Redshift views are "binding" by default to their underlying table which means you can't drop that
        underlying table without dropping the view first. This is a problem for us since we want to be able to
        swap tables out from under views. Therefore, we create the view as non-binding.
        """
        no_schema_binding = True
        if isinstance(query_or_df, exp.Expr):
            # We can't include NO SCHEMA BINDING if the query has a recursive CTE
            has_recursive_cte = any(
                w.args.get("recursive", False) for w in query_or_df.find_all(exp.With)
            )
            no_schema_binding = not has_recursive_cte

        return super().create_view(
            view_name,
            query_or_df,
            target_columns_to_types,
            replace,
            materialized,
            materialized_properties,
            table_description=table_description,
            column_descriptions=column_descriptions,
            no_schema_binding=no_schema_binding,
            view_properties=view_properties,
            source_columns=source_columns,
            **create_kwargs,
        )

    def _build_table_properties_exp(
        self,
        catalog_name: t.Optional[str] = None,
        table_format: t.Optional[str] = None,
        storage_format: t.Optional[str] = None,
        partitioned_by: t.Optional[t.List[exp.Expr]] = None,
        partition_interval_unit: t.Optional[IntervalUnit] = None,
        clustered_by: t.Optional[t.List[exp.Expr]] = None,
        table_properties: t.Optional[t.Dict[str, exp.Expr]] = None,
        target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        table_description: t.Optional[str] = None,
        table_kind: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        properties: t.List[exp.Expr] = []

        if table_description:
            properties.append(
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        def _to_identifier_if_string(expression: exp.Expr) -> exp.Expr:
            if isinstance(expression, exp.Literal) and expression.is_string:
                return exp.to_identifier(expression.this)
            return expression.copy()

        if table_properties:
            table_properties = {k.upper(): v for k, v in table_properties.items()}

            table_type = self._pop_creatable_type_from_properties(table_properties)
            properties.extend(ensure_list(table_type))

            diststyle = table_properties.get("DISTSTYLE")
            if diststyle:
                properties.append(exp.DistStyleProperty(this=exp.var(diststyle.name.upper())))

            distkey = table_properties.get("DISTKEY")
            if distkey:
                properties.append(exp.DistKeyProperty(this=_to_identifier_if_string(distkey)))

            sortkey = table_properties.get("SORTKEY")
            if sortkey:
                sortkey_expressions = sortkey.expressions if sortkey.expressions else [sortkey]
                properties.append(
                    exp.SortKeyProperty(
                        this=[
                            _to_identifier_if_string(expression)
                            for expression in sortkey_expressions
                        ],
                        compound=False,
                    )
                )

        return exp.Properties(expressions=properties) if properties else None

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
        """
        Redshift doesn't support `CREATE OR REPLACE TABLE...` and it also doesn't support `VALUES` expression so we need to specially
        handle DataFrame replacements.

        If the table doesn't exist then we just create it and load it with insert statements
        If it does exist then we need to do the:
            `CREATE TABLE...`, `INSERT INTO...`, `RENAME TABLE...`, `RENAME TABLE...`, DROP TABLE...`  dance.
        """
        import pandas as pd

        target_data_object = self.get_data_object(table_name)
        table_exists = target_data_object is not None
        if self.drop_data_object_on_type_mismatch(target_data_object, DataObjectType.TABLE):
            table_exists = False

        if not isinstance(query_or_df, pd.DataFrame) or not table_exists:
            return super().replace_query(
                table_name,
                query_or_df,
                target_columns_to_types,
                table_description,
                column_descriptions,
                source_columns=source_columns,
                **kwargs,
            )
        source_queries, target_columns_to_types = self._get_source_queries_and_columns_to_types(
            query_or_df,
            target_columns_to_types,
            target_table=table_name,
            source_columns=source_columns,
        )
        target_columns_to_types = target_columns_to_types or self.columns(table_name)
        target_table = exp.to_table(table_name)
        with self.transaction():
            temp_table = self._get_temp_table(target_table)
            old_table = self._get_temp_table(target_table)
            self.create_table(
                temp_table,
                target_columns_to_types,
                exists=False,
                table_description=table_description,
                column_descriptions=column_descriptions,
                **kwargs,
            )
            self._insert_append_source_queries(temp_table, source_queries, target_columns_to_types)
            self.rename_table(target_table, old_table)
            self.rename_table(temp_table, target_table)
            self.drop_table(old_table)

    def _get_data_objects(
        self, schema_name: SchemaName, object_names: t.Optional[t.Set[str]] = None
    ) -> t.List[DataObject]:
        """
        Returns all the data objects that exist in the given schema and optionally catalog.
        """
        catalog = self.get_current_catalog()
        table_query = exp.select(
            exp.column("schemaname").as_("schema_name"),
            exp.column("tablename").as_("name"),
            exp.Literal.string("TABLE").as_("type"),
        ).from_("pg_tables")
        view_query = (
            exp.select(
                exp.column("schemaname").as_("schema_name"),
                exp.column("viewname").as_("name"),
                exp.Literal.string("VIEW").as_("type"),
            )
            .from_("pg_views")
            .where(exp.column("definition").ilike("%create materialized view%").not_())
        )
        materialized_view_query = (
            exp.select(
                exp.column("schemaname").as_("schema_name"),
                exp.column("viewname").as_("name"),
                exp.Literal.string("MATERIALIZED_VIEW").as_("type"),
            )
            .from_("pg_views")
            .where(exp.column("definition").ilike("%create materialized view%"))
        )
        subquery = exp.union(
            table_query,
            exp.union(view_query, materialized_view_query, distinct=False),
            distinct=False,
        )
        query = (
            exp.select("*")
            .from_(subquery.subquery(alias="objs"))
            .where(exp.column("schema_name").eq(to_schema(schema_name).db))
        )
        if object_names:
            query = query.where(exp.column("name").isin(*object_names))
        df = self.fetchdf(query)
        return [
            DataObject(
                catalog=catalog,
                schema=row.schema_name,
                name=row.name,
                type=DataObjectType.from_str(row.type),  # type: ignore
            )
            for row in df.itertuples()
        ]

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
        if self.enable_merge:
            # By default we use the logical merge unless the user has opted in
            super().merge(
                target_table=target_table,
                source_table=source_table,
                target_columns_to_types=target_columns_to_types,
                unique_key=unique_key,
                when_matched=when_matched,
                merge_filter=merge_filter,
                source_columns=source_columns,
            )
        else:
            logical_merge(
                self,
                target_table,
                source_table,
                target_columns_to_types,
                unique_key,
                when_matched=when_matched,
                merge_filter=merge_filter,
                source_columns=source_columns,
            )

    def _merge(
        self,
        target_table: TableName,
        query: Query,
        on: exp.Expr,
        whens: exp.Whens,
    ) -> None:
        # Redshift does not support table aliases in the target table of a MERGE statement.
        # So we must use the actual table name instead of an alias, as we do with the source table.
        def resolve_target_table(expression: exp.Expr) -> exp.Expr:
            if (
                isinstance(expression, exp.Column)
                and expression.table.upper() == MERGE_TARGET_ALIAS
            ):
                expression.set("table", exp.to_table(target_table))
            return expression

        # Ensure that there is exactly one "WHEN MATCHED" and one "WHEN NOT MATCHED" clause.
        # Since Redshift does not support multiple "WHEN MATCHED" clauses.
        if (
            len(whens.expressions) != 2
            or whens.expressions[0].args["matched"] == whens.expressions[1].args["matched"]
        ):
            raise SQLMeshError(
                "Redshift only supports a single WHEN MATCHED and WHEN NOT MATCHED clause"
            )

        using = exp.alias_(
            exp.Subquery(this=query), alias=MERGE_SOURCE_ALIAS, copy=False, table=True
        )
        self.execute(
            exp.Merge(
                this=target_table,
                using=using,
                on=on.transform(resolve_target_table),
                whens=whens.transform(resolve_target_table),
            ),
            track_rows_processed=True,
        )

    def _normalize_decimal_value(self, expr: exp.Expr, precision: int) -> exp.Expr:
        # Redshift is finicky. It truncates when the data is already in a table, but rounds when the data is generated as part of a SELECT.
        #
        # The following works:
        #  > select cast(cast(3.14159 as decimal(6, 5)) as decimal(6, 3)); --produces '3.142', the value we want / what every other database produces
        #
        # However, if you write that to a table, and then cast it to a less precise decimal, you get _truncation_.
        #  > create table foo (val decimal(6, 5)); insert into foo(val) values (3.14159);
        #  > select cast(val as decimal(6, 3)) from foo; --produces '3.141'
        #
        # So to make up for this, we force it to round by injecting a round() expression
        rounded = exp.func("ROUND", expr, precision)

        return super()._normalize_decimal_value(rounded, precision)
