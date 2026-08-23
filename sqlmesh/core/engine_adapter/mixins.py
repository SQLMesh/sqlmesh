from __future__ import annotations

import abc
import logging
import typing as t
from dataclasses import dataclass

from sqlglot import exp, parse_one
from sqlglot.helper import seq_get
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sqlmesh.core.engine_adapter.base import EngineAdapter
from sqlmesh.core.engine_adapter.shared import DataObjectType
from sqlmesh.core.node import IntervalUnit
from sqlmesh.core.dialect import schema_
from sqlmesh.core.schema_diff import TableAlterOperation
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import TableName
    from sqlmesh.core.engine_adapter._typing import (
        DCL,
        DF,
        GrantsConfig,
        QueryOrDF,
    )
    from sqlmesh.core.engine_adapter.base import QueryOrDF

logger = logging.getLogger(__name__)

NORMALIZED_DATE_FORMAT = "%Y-%m-%d"
NORMALIZED_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


class LogicalMergeMixin(EngineAdapter):
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


class PandasNativeFetchDFSupportMixin(EngineAdapter):
    def _fetch_native_df(
        self, query: t.Union[exp.Expr, str], quote_identifiers: bool = False
    ) -> DF:
        """Fetches a Pandas DataFrame from a SQL query."""
        from warnings import catch_warnings, filterwarnings

        from pandas.io.sql import read_sql_query

        sql = self._to_sql(query, quote=quote_identifiers) if isinstance(query, exp.Expr) else query
        logger.debug(f"Executing SQL:\n{sql}")
        with catch_warnings(), self.transaction():
            filterwarnings(
                "ignore",
                category=UserWarning,
                message=".*pandas only supports SQLAlchemy connectable.*",
            )
            df = read_sql_query(sql, self._connection_pool.get())
        return df


class HiveMetastoreTablePropertiesMixin(EngineAdapter):
    MAX_TABLE_COMMENT_LENGTH = 4000
    MAX_COLUMN_COMMENT_LENGTH = 4000

    def _build_partitioned_by_exp(
        self,
        partitioned_by: t.List[exp.Expr],
        *,
        catalog_name: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Union[exp.PartitionedByProperty, exp.Property]:
        if (
            self.dialect == "trino"
            and self.get_catalog_type(catalog_name or self.get_current_catalog()) == "iceberg"
        ):
            # On the Trino Iceberg catalog, the table property is called "partitioning" - not "partitioned_by"
            # In addition, partition column transform expressions like `day(col)` or `bucket(col, 5)` are allowed
            # Also, column names and transforms need to be strings and supplied as an ARRAY[varchar]
            # ref: https://trino.io/docs/current/connector/iceberg.html#table-properties
            return exp.Property(
                this=exp.var("PARTITIONING"),
                value=exp.array(
                    *(exp.Literal.string(e.sql(dialect=self.dialect)) for e in partitioned_by)
                ),
            )
        for expr in partitioned_by:
            if not isinstance(expr, exp.Column):
                raise SQLMeshError(
                    f"PARTITIONED BY contains non-column value '{expr.sql(dialect=self.dialect)}'."
                )
        return exp.PartitionedByProperty(
            this=exp.Schema(expressions=partitioned_by),
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

        if table_format and self.dialect == "spark":
            properties.append(exp.FileFormatProperty(this=exp.Var(this=table_format)))
            if storage_format:
                properties.append(
                    exp.Property(
                        this="write.format.default", value=exp.Literal.string(storage_format)
                    )
                )
        elif storage_format:
            properties.append(exp.FileFormatProperty(this=exp.Var(this=storage_format)))

        if partitioned_by:
            properties.append(
                self._build_partitioned_by_exp(
                    partitioned_by,
                    partition_interval_unit=partition_interval_unit,
                    catalog_name=catalog_name,
                )
            )

        if table_description:
            properties.append(
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        properties.extend(self._table_or_view_properties_to_expressions(table_properties))

        if properties:
            return exp.Properties(expressions=properties)
        return None

    def _build_view_properties_exp(
        self,
        view_properties: t.Optional[t.Dict[str, exp.Expr]] = None,
        table_description: t.Optional[str] = None,
        **kwargs: t.Any,
    ) -> t.Optional[exp.Properties]:
        """Creates a SQLGlot table properties expression for view"""
        properties: t.List[exp.Expr] = []

        if table_description:
            properties.append(
                exp.SchemaCommentProperty(
                    this=exp.Literal.string(self._truncate_table_comment(table_description))
                )
            )

        properties.extend(self._table_or_view_properties_to_expressions(view_properties))

        if properties:
            return exp.Properties(expressions=properties)
        return None

    def _truncate_comment(self, comment: str, length: t.Optional[int]) -> str:
        # iceberg and delta do not have a comment length limit
        if self.current_catalog_type in ("iceberg", "delta_lake"):
            return comment
        return super()._truncate_comment(comment, length)


class GetCurrentCatalogFromFunctionMixin(EngineAdapter):
    CURRENT_CATALOG_EXPRESSION: exp.Expr = exp.func("current_catalog")

    def get_current_catalog(self) -> t.Optional[str]:
        """Returns the catalog name of the current connection."""
        result = self.fetchone(exp.select(self.CURRENT_CATALOG_EXPRESSION))
        if result:
            return result[0]
        return None


class NonTransactionalTruncateMixin(EngineAdapter):
    def _truncate_table(self, table_name: TableName) -> None:
        # Truncate forces a commit of the current transaction so we want to do an unconditional delete to
        # preserve the transaction if one exists otherwise we can truncate
        if self._connection_pool.is_transaction_active:
            return self.execute(exp.Delete(this=exp.to_table(table_name)))
        super()._truncate_table(table_name)


class VarcharSizeWorkaroundMixin(EngineAdapter):
    def _default_precision_to_max(
        self, columns_to_types: t.Dict[str, exp.DataType]
    ) -> t.Dict[str, exp.DataType]:
        # get default lengths for types that support "max" length
        types_with_max_default_param = {
            k: [self.schema_differ.parameterized_type_defaults[k][0][0]]
            for k in self.schema_differ.max_parameter_length
            if k in self.schema_differ.parameterized_type_defaults
        }

        # Redshift and MSSQL have a bug where CTAS statements have non-deterministic types. If a LIMIT
        # is applied to a CTAS statement, VARCHAR (and possibly other) types sometimes revert to their
        # default length of 256 (Redshift) or 1 (MSSQL). If we detect that a type has its default length
        # and supports "max" length, we convert it to "max" length to prevent inadvertent data truncation.
        for col_name, col_type in columns_to_types.items():
            if col_type.this in types_with_max_default_param and col_type.expressions:
                parameter = self.schema_differ.get_type_parameters(col_type)
                type_default = types_with_max_default_param[col_type.this]
                if parameter == type_default:
                    col_type.set("expressions", [exp.DataTypeParam(this=exp.var("max"))])

        return columns_to_types

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
        statement = super()._build_create_table_exp(
            table_name_or_schema,
            expression=expression,
            exists=exists,
            replace=replace,
            target_columns_to_types=target_columns_to_types,
            table_description=table_description,
            table_kind=table_kind,
            **kwargs,
        )

        if (
            statement.expression
            and statement.expression.args.get("limit") is not None
            and statement.expression.args["limit"].expression.this == "0"
        ):
            assert not isinstance(table_name_or_schema, exp.Schema)

            # redshift and mssql have a bug where CTAS statements have non determistic types. if a limit
            # is applied to a ctas statement, VARCHAR types default to 1 in some instances.
            select_statement = statement.expression.copy()
            for select_or_union in select_statement.find_all(exp.Select, exp.SetOperation):
                limit = select_or_union.args.get("limit")
                if limit is not None and limit.expression.this == "0":
                    limit.pop()

                select_or_union.set("where", None)

            temp_view_name = self._get_temp_table("ctas")

            self.create_view(temp_view_name, select_statement, replace=False)
            try:
                columns_to_types_from_view = self._default_precision_to_max(
                    self.columns(temp_view_name)
                )

                schema = self._build_schema_exp(
                    exp.to_table(table_name_or_schema),
                    columns_to_types_from_view,
                )
                statement = super()._build_create_table_exp(
                    schema,
                    None,
                    exists=exists,
                    replace=replace,
                    target_columns_to_types=columns_to_types_from_view,
                    table_description=table_description,
                    **kwargs,
                )
            finally:
                self.drop_view(temp_view_name)

        return statement


@dataclass(frozen=True)
class TableAlterClusterByOperation(TableAlterOperation, abc.ABC):
    pass


@dataclass(frozen=True)
class TableAlterChangeClusterKeyOperation(TableAlterClusterByOperation):
    clustering_key: str
    dialect: str

    @property
    def is_additive(self) -> bool:
        return False

    @property
    def is_destructive(self) -> bool:
        return False

    @property
    def _alter_actions(self) -> t.List[exp.Expr]:
        return [exp.Cluster(expressions=self.cluster_key_expressions)]

    @property
    def cluster_key_expressions(self) -> t.List[exp.Expr]:
        # Note: Assumes `clustering_key` as a string like:
        # - "(col_a)"
        # - "(col_a, col_b)"
        # - "func(col_a, transform(col_b))"
        parsed_cluster_key = parse_one(self.clustering_key, dialect=self.dialect)
        return parsed_cluster_key.expressions or [parsed_cluster_key.this]


@dataclass(frozen=True)
class TableAlterDropClusterKeyOperation(TableAlterClusterByOperation):
    @property
    def is_additive(self) -> bool:
        return False

    @property
    def is_destructive(self) -> bool:
        return False

    @property
    def _alter_actions(self) -> t.List[exp.Expr]:
        return [exp.Command(this="DROP", expression="CLUSTERING KEY")]


class ClusteredByMixin(EngineAdapter):
    def _build_clustered_by_exp(
        self,
        clustered_by: t.List[exp.Expr],
        **kwargs: t.Any,
    ) -> t.Optional[exp.Cluster]:
        return exp.Cluster(expressions=[c.copy() for c in clustered_by])

    def get_alter_operations(
        self,
        current_table_name: TableName,
        target_table_name: TableName,
        *,
        ignore_destructive: bool = False,
        ignore_additive: bool = False,
    ) -> t.List[TableAlterOperation]:
        operations = super().get_alter_operations(
            current_table_name,
            target_table_name,
            ignore_destructive=ignore_destructive,
            ignore_additive=ignore_additive,
        )

        # check for a change in clustering
        current_table = exp.to_table(current_table_name)
        target_table = exp.to_table(target_table_name)

        current_table_schema = schema_(current_table.db, catalog=current_table.catalog)
        target_table_schema = schema_(target_table.db, catalog=target_table.catalog)

        current_table_info = seq_get(
            self.get_data_objects(current_table_schema, {current_table.name}), 0
        )
        target_table_info = seq_get(
            self.get_data_objects(target_table_schema, {target_table.name}), 0
        )

        if current_table_info and target_table_info:
            if target_table_info.is_clustered:
                if target_table_info.clustering_key and (
                    current_table_info.clustering_key != target_table_info.clustering_key
                ):
                    operations.append(
                        TableAlterChangeClusterKeyOperation(
                            target_table=current_table,
                            clustering_key=target_table_info.clustering_key,
                            dialect=self.dialect,
                        )
                    )
            elif current_table_info.is_clustered:
                operations.append(TableAlterDropClusterKeyOperation(target_table=current_table))

        return operations


def logical_merge(
    engine_adapter: EngineAdapter,
    target_table: TableName,
    source_table: QueryOrDF,
    target_columns_to_types: t.Optional[t.Dict[str, exp.DataType]],
    unique_key: t.Sequence[exp.Expr],
    when_matched: t.Optional[exp.Whens] = None,
    merge_filter: t.Optional[exp.Expr] = None,
    source_columns: t.Optional[t.List[str]] = None,
) -> None:
    """
    Merge implementation for engine adapters that do not support merge natively.

    The merge is executed as follows:
    1. Create a temporary table containing the new data to merge.
    2. Delete rows from target table where unique_key cols match a row in the temporary table.
    3. Insert the temporary table contents into the target table. Any duplicate, non-unique rows
       within the temporary table are ommitted.
    4. Drop the temporary table.
    """
    if when_matched or merge_filter:
        prop = "when_matched" if when_matched else "merge_filter"
        raise SQLMeshError(
            f"This engine does not support MERGE expressions and therefore `{prop}` is not supported."
        )

    engine_adapter._replace_by_key(
        target_table,
        source_table,
        target_columns_to_types,
        unique_key,
        is_unique_key=True,
        source_columns=source_columns,
    )


class RowDiffMixin(EngineAdapter):
    # The maximum supported value for n in timestamp(n).
    # Most databases are microsecond (6) but some can only handle millisecond (3) while others go to nanosecond (9)
    MAX_TIMESTAMP_PRECISION = 6

    def concat_columns(
        self,
        columns_to_types: t.Dict[str, exp.DataType],
        decimal_precision: int = 3,
        timestamp_precision: int = MAX_TIMESTAMP_PRECISION,
        delimiter: str = ",",
    ) -> exp.Expr:
        """
        Produce an expression that generates a string version of a record, that is:
            - Every column converted to a string representation, joined together into a single string using the specified :delimiter
        """
        expressions_to_concat: t.List[exp.Expr] = []
        for idx, (column, type) in enumerate(columns_to_types.items()):
            expressions_to_concat.append(
                exp.func(
                    "COALESCE",
                    self.normalize_value(
                        exp.to_column(column), type, decimal_precision, timestamp_precision
                    ),
                    exp.Literal.string(""),
                )
            )
            if idx < len(columns_to_types) - 1:
                expressions_to_concat.append(exp.Literal.string(delimiter))

        return exp.func("CONCAT", *expressions_to_concat)

    def normalize_value(
        self,
        expr: exp.Expr,
        type: exp.DataType,
        decimal_precision: int = 3,
        timestamp_precision: int = MAX_TIMESTAMP_PRECISION,
    ) -> exp.Expr:
        """
        Return an expression that converts the values inside the column `col` to a normalized string

        This string should be comparable across database engines, eg:
            - `date` columns -> YYYY-MM-DD string
            - `datetime`/`timestamp`/`timestamptz` columns -> ISO-8601 string to :timestamp_precision digits of subsecond precision
            - `float` / `double` / `decimal` -> Value formatted to :decimal_precision decimal places
            - `boolean` columns -> '1' or '0'
            - NULLS -> "" (empty string)
        """
        value: exp.Expr
        if type.is_type(exp.DataType.Type.BOOLEAN):
            value = self._normalize_boolean_value(expr)
        elif type.is_type(*exp.DataType.INTEGER_TYPES):
            value = self._normalize_integer_value(expr)
        elif type.is_type(*exp.DataType.REAL_TYPES):
            # If there is no scale on the decimal type, treat it like an integer when comparing
            # Some databases like Snowflake deliberately create all integer types as NUMERIC(<size>, 0)
            # and they should be treated as integers and not decimals
            type_params = list(type.find_all(exp.DataTypeParam))
            if len(type_params) == 2 and type_params[-1].this.to_py() == 0:
                value = self._normalize_integer_value(expr)
            else:
                value = self._normalize_decimal_value(expr, decimal_precision)
        elif type.is_type(*exp.DataType.TEMPORAL_TYPES):
            value = self._normalize_timestamp_value(expr, type, timestamp_precision)
        elif type.is_type(*exp.DataType.NESTED_TYPES):
            value = self._normalize_nested_value(expr)
        else:
            value = expr

        return exp.cast(value, to=exp.DataType.build("VARCHAR"))

    def _normalize_nested_value(self, expr: exp.Expr) -> exp.Expr:
        return expr

    def _normalize_timestamp_value(
        self, expr: exp.Expr, type: exp.DataType, precision: int
    ) -> exp.Expr:
        if precision > self.MAX_TIMESTAMP_PRECISION:
            raise ValueError(
                f"Requested timestamp precision '{precision}' exceeds maximum supported precision: {self.MAX_TIMESTAMP_PRECISION}"
            )

        is_date = type.is_type(exp.DataType.Type.DATE, exp.DataType.Type.DATE32)

        format = NORMALIZED_DATE_FORMAT if is_date else NORMALIZED_TIMESTAMP_FORMAT

        if type.is_type(
            exp.DataType.Type.TIMESTAMPTZ,
            exp.DataType.Type.TIMESTAMPLTZ,
            exp.DataType.Type.TIMESTAMPNTZ,
        ):
            # Convert all timezone-aware values to UTC for comparison
            expr = exp.AtTimeZone(this=expr, zone=exp.Literal.string("UTC"))

        digits_to_chop_off = (
            6 - precision
        )  # 6 = max precision across all adapters and also the max amount of digits TimeToStr will render since its based on `strftime` and `%f` only renders to microseconds

        expr = exp.TimeToStr(this=expr, format=exp.Literal.string(format))
        if digits_to_chop_off > 0:
            expr = exp.func(
                "SUBSTRING", expr, 1, len("2023-01-01 12:13:14.000000") - digits_to_chop_off
            )

        return expr

    def _normalize_integer_value(self, expr: exp.Expr) -> exp.Expr:
        return exp.cast(expr, "BIGINT")

    def _normalize_decimal_value(self, expr: exp.Expr, precision: int) -> exp.Expr:
        return exp.cast(expr, f"DECIMAL(38,{precision})")

    def _normalize_boolean_value(self, expr: exp.Expr) -> exp.Expr:
        return exp.cast(expr, "INT")


class GrantsFromInfoSchemaMixin(EngineAdapter):
    CURRENT_USER_OR_ROLE_EXPRESSION: exp.Expr = exp.func("current_user")
    SUPPORTS_MULTIPLE_GRANT_PRINCIPALS = False
    USE_CATALOG_IN_GRANTS = False
    GRANT_INFORMATION_SCHEMA_TABLE_NAME = "table_privileges"

    @staticmethod
    @abc.abstractmethod
    def _grant_object_kind(table_type: DataObjectType) -> t.Optional[str]:
        pass

    @abc.abstractmethod
    def _get_current_schema(self) -> str:
        pass

    def _dcl_grants_config_expr(
        self,
        dcl_cmd: t.Type[DCL],
        table: exp.Table,
        grants_config: GrantsConfig,
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> t.List[exp.Expr]:
        expressions: t.List[exp.Expr] = []
        if not grants_config:
            return expressions

        object_kind = self._grant_object_kind(table_type)
        for privilege, principals in grants_config.items():
            args: t.Dict[str, t.Any] = {
                "privileges": [exp.GrantPrivilege(this=exp.Var(this=privilege))],
                "securable": table.copy(),
            }
            if object_kind:
                args["kind"] = exp.Var(this=object_kind)
            if self.SUPPORTS_MULTIPLE_GRANT_PRINCIPALS:
                args["principals"] = [
                    normalize_identifiers(
                        parse_one(principal, into=exp.GrantPrincipal, dialect=self.dialect),
                        dialect=self.dialect,
                    )
                    for principal in principals
                ]
                expressions.append(dcl_cmd(**args))  # type: ignore[arg-type]
            else:
                for principal in principals:
                    args["principals"] = [
                        normalize_identifiers(
                            parse_one(principal, into=exp.GrantPrincipal, dialect=self.dialect),
                            dialect=self.dialect,
                        )
                    ]
                    expressions.append(dcl_cmd(**args))  # type: ignore[arg-type]

        return expressions

    def _apply_grants_config_expr(
        self,
        table: exp.Table,
        grants_config: GrantsConfig,
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> t.List[exp.Expr]:
        return self._dcl_grants_config_expr(exp.Grant, table, grants_config, table_type)

    def _revoke_grants_config_expr(
        self,
        table: exp.Table,
        grants_config: GrantsConfig,
        table_type: DataObjectType = DataObjectType.TABLE,
    ) -> t.List[exp.Expr]:
        return self._dcl_grants_config_expr(exp.Revoke, table, grants_config, table_type)

    def _get_grant_expression(self, table: exp.Table) -> exp.Expr:
        schema_identifier = table.args.get("db") or normalize_identifiers(
            exp.to_identifier(self._get_current_schema(), quoted=True), dialect=self.dialect
        )
        schema_name = schema_identifier.this
        table_name = table.args.get("this").this  # type: ignore

        grant_conditions = [
            exp.column("table_schema").eq(exp.Literal.string(schema_name)),
            exp.column("table_name").eq(exp.Literal.string(table_name)),
            exp.column("grantor").eq(self.CURRENT_USER_OR_ROLE_EXPRESSION),
            exp.column("grantee").neq(self.CURRENT_USER_OR_ROLE_EXPRESSION),
        ]

        info_schema_table = normalize_identifiers(
            exp.table_(self.GRANT_INFORMATION_SCHEMA_TABLE_NAME, db="information_schema"),
            dialect=self.dialect,
        )
        if self.USE_CATALOG_IN_GRANTS:
            catalog_identifier = table.args.get("catalog")
            if not catalog_identifier:
                catalog_name = self.get_current_catalog()
                if not catalog_name:
                    raise SQLMeshError(
                        "Current catalog could not be determined for fetching grants. This is unexpected."
                    )
                catalog_identifier = normalize_identifiers(
                    exp.to_identifier(catalog_name, quoted=True), dialect=self.dialect
                )
            catalog_name = catalog_identifier.this
            info_schema_table.set("catalog", catalog_identifier.copy())
            grant_conditions.insert(
                0, exp.column("table_catalog").eq(exp.Literal.string(catalog_name))
            )

        return (
            exp.select("privilege_type", "grantee")
            .from_(info_schema_table)
            .where(exp.and_(*grant_conditions))
        )

    def _get_current_grants_config(self, table: exp.Table) -> GrantsConfig:
        grant_expr = self._get_grant_expression(table)

        results = self.fetchall(grant_expr)

        grants_dict: GrantsConfig = {}
        for privilege_raw, grantee_raw in results:
            if privilege_raw is None or grantee_raw is None:
                continue

            privilege = str(privilege_raw)
            grantee = str(grantee_raw)
            if not privilege or not grantee:
                continue

            grantees = grants_dict.setdefault(privilege, [])
            if grantee not in grantees:
                grantees.append(grantee)

        return grants_dict
